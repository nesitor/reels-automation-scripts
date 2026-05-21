"""Stage 4 — submit every clip's cropped image + video prompt to ComfyUI.

Submission is fast (~ms per clip). ComfyUI processes its queue serially while
you sleep. The actual progress is tracked by stage5_poll_videos.py.

Idempotent: only enqueues clips with video_status='pending' (or 'failed' if
--retry is set) and image ready. Use --force to also re-enqueue clips stuck
in 'queued'/'running' (e.g. after cancelling the ComfyUI queue mid-flight);
--force additionally ignores the VIDEO_MAX_ATTEMPTS cap. 'done' clips are
never touched — use scripts/redo_clips.py for those.

Usage:
    python scripts/stage4_queue_videos.py --run-id 1
    python scripts/stage4_queue_videos.py --run-id 1 --retry        # re-enqueue failed
    python scripts/stage4_queue_videos.py --run-id 1 --force        # re-enqueue queued/running too
    python scripts/stage4_queue_videos.py --run-id 1 --clips 7      # only clip 7
    python scripts/stage4_queue_videos.py --run-id 1 --clips 7-     # from 7 to end
    python scripts/stage4_queue_videos.py --run-id 1 --clips 5-10   # clips 5..10
    python scripts/stage4_queue_videos.py --run-id 1 --clips 7,9,12 # cherry-pick
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import comfyui_client, db, paths, telegram
from lib.config import CFG
from lib.logger import event, get

log = get("stage4")


def _parse_clip_filter(spec: str | None) -> set[int] | None:
    """Parse a clip-order selector. Returns None when spec is empty.

    Accepts comma-separated tokens, each either a single int or a range.
    Open-ended ranges default to 1 / 9999.

      "7"        → {7}
      "7,9,12"   → {7, 9, 12}
      "7-"       → {7, 8, ..., 9999}
      "5-10"     → {5, 6, ..., 10}
      "-3"       → {1, 2, 3}
    """
    if not spec:
        return None
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, hi_s = chunk.split("-", 1)
            lo = int(lo_s) if lo_s.strip() else 1
            hi = int(hi_s) if hi_s.strip() else 9999
            if lo > hi:
                raise ValueError(f"invalid range {chunk!r}")
            out.update(range(lo, hi + 1))
        else:
            out.add(int(chunk))
    return out


def _pending(run_id: int, retry: bool, force: bool,
             clip_filter: set[int] | None) -> list[dict]:
    if force:
        # --force re-enqueues anything not already downloaded — including
        # clips left in 'queued'/'running' after the ComfyUI queue was
        # cancelled. 'done' is still skipped (use redo_clips.py for those).
        statuses = ("pending", "failed", "queued", "running")
    elif retry:
        statuses = ("pending", "failed")
    else:
        statuses = ("pending",)
    sql = (
        "SELECT id, clip_order, video_prompt, video_prompt_negative, "
        "image_cropped_path, video_attempts FROM clips "
        f"WHERE run_id=? AND image_cropped_path IS NOT NULL "
        f"AND video_status IN ({','.join('?' * len(statuses))}) "
    )
    params: list = [run_id, *statuses]
    if not force:
        # --force also ignores the per-clip attempts cap: a cancelled queue
        # is not a generation failure and shouldn't burn the retry budget.
        sql += "AND video_attempts < ? "
        params.append(CFG.video_max_attempts)
    if clip_filter:
        sql += f"AND clip_order IN ({','.join('?' * len(clip_filter))}) "
        params.extend(sorted(clip_filter))
    sql += "ORDER BY clip_order"
    with db.connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _run_meta(run_id: int) -> tuple[int, int]:
    with db.connect(readonly=True) as conn:
        row = conn.execute("SELECT guion_id FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise SystemExit(f"run_id {run_id} not found")
    with db.connect(readonly=True) as conn:
        gv = conn.execute("SELECT version FROM guiones WHERE id=?", (row["guion_id"],)).fetchone()
    return row["guion_id"], gv["version"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--retry", action="store_true",
                        help="Also re-enqueue clips previously marked failed.")
    parser.add_argument("--force", action="store_true",
                        help="Also re-enqueue clips stuck in 'queued'/'running' "
                             "(e.g. after cancelling the ComfyUI queue). Ignores "
                             "the VIDEO_MAX_ATTEMPTS cap. 'done' clips are still "
                             "skipped — use redo_clips.py for those.")
    parser.add_argument("--clips", default=None,
                        help='Filter by clip_order. Examples: "7", "7,9,12", '
                             '"7-" (from 7 to end), "5-10", "-3".')
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    clip_filter = _parse_clip_filter(args.clips)
    guion_id, version = _run_meta(args.run_id)
    pending = _pending(args.run_id, args.retry, args.force, clip_filter)
    if args.force:
        log.warning("--force: re-enqueuing clips regardless of current status; "
                    "make sure the ComfyUI queue is actually clear so you don't "
                    "stack duplicate jobs")
    if not pending:
        msg = "nothing to enqueue"
        if clip_filter:
            msg += f" (clip filter applied: {sorted(clip_filter)[:10]}{'…' if len(clip_filter)>10 else ''})"
        log.info(msg)
        return 0

    log.info("enqueuing %d clips on ComfyUI at %s (orders=%s)",
             len(pending), CFG.comfyui_host, [c["clip_order"] for c in pending])
    n_ok, n_fail = 0, 0
    for c in pending:
        prefix = f"v{version}_run{args.run_id}_clip{c['clip_order']:02d}"
        seed = random.randint(1, 2**31 - 1)
        try:
            workflow = comfyui_client.build_prompt(
                image_path=paths.resolve(c["image_cropped_path"]),
                positive_prompt=c["video_prompt"],
                negative_prompt=c["video_prompt_negative"],
                seed=seed,
                output_filename_prefix=prefix,
            )
            prompt_id = comfyui_client.submit(workflow)
        except Exception as exc:  # noqa: BLE001
            with db.connect() as conn:
                conn.execute(
                    "UPDATE clips SET video_status='failed', video_attempts=video_attempts+1, "
                    "video_error=? WHERE id=?",
                    (str(exc), c["id"]),
                )
            event("stage4", f"clip {c['clip_order']} submit failed: {exc}",
                  run_id=args.run_id, clip_id=c["id"], level="error")
            n_fail += 1
            continue

        with db.connect() as conn:
            conn.execute(
                "UPDATE clips SET video_status='queued', comfyui_prompt_id=?, "
                "video_attempts=video_attempts+1, video_error=NULL WHERE id=?",
                (prompt_id, c["id"]),
            )
        event("stage4", f"clip {c['clip_order']} queued prompt_id={prompt_id}",
              run_id=args.run_id, clip_id=c["id"],
              payload={"prefix": prefix, "seed": seed})
        n_ok += 1

    log.info("stage4 done: queued=%d failed=%d", n_ok, n_fail)
    if args.notify:
        telegram.send(
            ("stage4_done", str(args.run_id), str(n_ok)),
            f"🎬 *Stage 4 complete* — run `{args.run_id}`\n"
            f"queued: ✅ {n_ok}  ❌ {n_fail}\n"
            f"Now run `stage5_poll_videos.py --run-id {args.run_id} --notify` "
            f"in the background.",
        )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
