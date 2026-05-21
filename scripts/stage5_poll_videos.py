"""Stage 5 — long-running daemon that polls ComfyUI for finished video clips.

Reconciles each queued clip's state with ComfyUI's queue/history:
  - 'queued'/'running' → keep polling
  - 'done'              → copy MP4 into run output_dir, mark video_status='done'
  - 'failed'            → record error, mark video_status='failed'

Sends per-clip Telegram pings on terminal transitions, and a final summary
when no clip in the run is queued/running anymore.

Designed to run for hours. Survives Ctrl+C and SIGTERM cleanly by simply
exiting after the current loop iteration (state is in SQLite so resume is
free — re-launching reconciles from where ComfyUI is now).

Usage:
    python scripts/stage5_poll_videos.py --run-id 1 --notify
    nohup python scripts/stage5_poll_videos.py --run-id 1 --notify &> stage5.log &
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import comfyui_client, db, paths, telegram
from lib.config import CFG
from lib.logger import event, get

log = get("stage5")
_should_stop = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _handle_signal(signum, _frame) -> None:
    global _should_stop
    _should_stop = True
    log.warning("signal %d received, will exit after current loop", signum)


def _active_clips(run_id: int) -> list[dict]:
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, clip_order, comfyui_prompt_id, video_status "
            "FROM clips WHERE run_id=? AND video_status IN ('queued','running') "
            "AND comfyui_prompt_id IS NOT NULL ORDER BY clip_order",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _run_output_dir(run_id: int) -> Path:
    with db.connect(readonly=True) as conn:
        row = conn.execute("SELECT output_dir FROM runs WHERE id=?", (run_id,)).fetchone()
    return paths.resolve(row["output_dir"])


def _on_done(run_id: int, clip: dict, status: comfyui_client.QueueStatus, prefix: str,
             videos_dir: Path, notify: bool) -> None:
    info = comfyui_client.find_output_in_history(status.outputs, prefix)
    if not info:
        # ComfyUI reports done but the outputs dict has no matching file yet.
        # Stay in 'running' so the next poll retries the lookup.
        with db.connect() as conn:
            conn.execute(
                "UPDATE clips SET video_status='running' WHERE id=? AND video_status='queued'",
                (clip["id"],),
            )
        log.warning("clip %d marked done by ComfyUI but matching output not found yet",
                    clip["clip_order"])
        return

    videos_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(info["filename"]).suffix or ".mp4"
    target = videos_dir / f"clip_{clip['clip_order']:02d}{ext}"
    try:
        comfyui_client.download_output(info, target)
    except Exception as exc:  # noqa: BLE001 — retry on next poll
        log.warning("download failed for clip %d: %s — will retry next poll",
                    clip["clip_order"], exc)
        with db.connect() as conn:
            conn.execute(
                "UPDATE clips SET video_status='running' WHERE id=? AND video_status='queued'",
                (clip["id"],),
            )
        return

    with db.connect() as conn:
        conn.execute(
            "UPDATE clips SET video_status='done', video_path=?, "
            "video_completed_at=? WHERE id=?",
            (paths.rel(target), _now(), clip["id"]),
        )
    event("stage5", f"clip {clip['clip_order']} done → {target.name}",
          run_id=run_id, clip_id=clip["id"])
    if notify:
        telegram.send(
            ("clip_done", str(run_id), str(clip["clip_order"])),
            f"✅ Clip {clip['clip_order']:02d} done — run `{run_id}`",
        )


def _on_failed(run_id: int, clip: dict, status: comfyui_client.QueueStatus, notify: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE clips SET video_status='failed', video_error=? WHERE id=?",
            (status.error or "unknown error", clip["id"]),
        )
    event("stage5", f"clip {clip['clip_order']} FAILED: {status.error}",
          run_id=run_id, clip_id=clip["id"], level="error")
    if notify:
        telegram.send(
            ("clip_failed", str(run_id), str(clip["clip_order"])),
            f"❌ Clip {clip['clip_order']:02d} FAILED — run `{run_id}`\n"
            f"```\n{(status.error or '')[:400]}\n```",
        )


def _final_summary(run_id: int, notify: bool) -> None:
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN video_status='done' THEN 1 ELSE 0 END) AS ok,"
            "  SUM(CASE WHEN video_status='failed' THEN 1 ELSE 0 END) AS fail,"
            "  COUNT(*) AS total "
            "FROM clips WHERE run_id=?",
            (run_id,),
        ).fetchone()
    log.info("run %d totals: ok=%d failed=%d total=%d",
             run_id, row["ok"], row["fail"], row["total"])
    if notify:
        telegram.send(
            ("run_complete", str(run_id), str(row["ok"]), str(row["fail"])),
            f"🏁 *Run `{run_id}` complete*\n"
            f"✅ {row['ok']}  ❌ {row['fail']}  /  {row['total']}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--interval", type=int, default=CFG.poll_interval_s)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--once", action="store_true",
                        help="Run one reconciliation pass and exit (useful for cron).")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    out_dir = _run_output_dir(args.run_id)
    videos_dir = out_dir / "videos"

    log.info("polling run_id=%d every %ds (notify=%s)",
             args.run_id, args.interval, args.notify)
    while not _should_stop:
        active = _active_clips(args.run_id)
        if not active:
            log.info("no active clips; run finished or none queued")
            _final_summary(args.run_id, args.notify)
            return 0

        for clip in active:
            try:
                st = comfyui_client.status(clip["comfyui_prompt_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("status() failed for clip %d: %s", clip["clip_order"], exc)
                continue

            if st.state == "running" and clip["video_status"] != "running":
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE clips SET video_status='running', video_started_at=COALESCE(video_started_at, ?) "
                        "WHERE id=?",
                        (_now(), clip["id"]),
                    )
                if args.notify:
                    telegram.send(
                        ("clip_started", str(args.run_id), str(clip["clip_order"])),
                        f"⏳ Clip {clip['clip_order']:02d} started — run `{args.run_id}`",
                    )
            elif st.state == "done":
                prefix = f"v_run{args.run_id}_clip{clip['clip_order']:02d}"
                # Actually rebuild prefix the same way stage4 did:
                with db.connect(readonly=True) as conn:
                    gv = conn.execute(
                        "SELECT g.version FROM guiones g JOIN runs r ON r.guion_id=g.id "
                        "WHERE r.id=?", (args.run_id,),
                    ).fetchone()
                prefix = f"v{gv['version']}_run{args.run_id}_clip{clip['clip_order']:02d}"
                _on_done(args.run_id, clip, st, prefix, videos_dir, args.notify)
            elif st.state == "failed":
                _on_failed(args.run_id, clip, st, args.notify)

        if args.once:
            return 0
        for _ in range(args.interval):
            if _should_stop:
                break
            time.sleep(1)

    _final_summary(args.run_id, args.notify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
