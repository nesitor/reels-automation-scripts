"""Re-queue specific clips for video (and optionally image) regeneration.

Resets the chosen clips' state in the DB so subsequent stages will treat
them as `pending` again, optionally backs up any existing MP4s, and chains
stage2 → stage3 → stage4 as needed. Idempotent: re-running is safe.

Usage:
    # Redo just the video for clips 3, 7, 11 (keep existing image)
    python scripts/redo_clips.py --run-id 1 --clips 3,7,11

    # Redo a range, backing up old MP4s first
    python scripts/redo_clips.py --run-id 1 --clips 5-10 --backup

    # Redo image + video (regenerates first frame and re-crops too)
    python scripts/redo_clips.py --run-id 1 --clips 7 --scope image,video

    # Same as above (shorthand)
    python scripts/redo_clips.py --run-id 1 --clips 7 --scope all

    # Only reset state — don't enqueue. You'll run stage4 yourself later.
    python scripts/redo_clips.py --run-id 1 --clips 3 --no-requeue

    # Preview without writing anything
    python scripts/redo_clips.py --run-id 1 --clips 3 --dry-run

Clip selector syntax (same as stage4_queue_videos.py):
    "7"        single clip
    "3,7,11"   cherry-pick
    "5-10"     range inclusive
    "7-"       from 7 to end
    "-3"       up to 3
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, paths
from lib.config import CFG
from lib.logger import event, get

log = get("redo")
ROOT = CFG.root
PY = sys.executable


def _parse_clip_filter(spec: str) -> set[int]:
    """Same syntax as stage4_queue_videos.py."""
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


def _parse_scope(spec: str) -> set[str]:
    parts = {p.strip().lower() for p in spec.split(",") if p.strip()}
    if "all" in parts:
        parts = {"image", "video"}
    unknown = parts - {"image", "video"}
    if unknown:
        raise SystemExit(
            f"unknown scope(s): {sorted(unknown)}. Use image, video, or all."
        )
    if not parts:
        raise SystemExit("--scope cannot be empty")
    return parts


def _select_clips(run_id: int, orders: set[int]) -> list[dict]:
    placeholders = ",".join("?" * len(orders))
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            f"SELECT id, clip_order, image_status, image_path, image_cropped_path, "
            f"video_status, video_path "
            f"FROM clips WHERE run_id=? AND clip_order IN ({placeholders}) "
            f"ORDER BY clip_order",
            (run_id, *sorted(orders)),
        ).fetchall()
    return [dict(r) for r in rows]


# Container extensions a clip's video file might use (LTX emits .mp4).
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".gif")


def _output_dir(run_id: int) -> Path:
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT output_dir FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"run_id {run_id} not found")
    return paths.resolve(row["output_dir"])


def _find_clip_video(output_dir: Path, clip: dict) -> Path | None:
    """Locate a clip's existing video file on disk.

    Prefers the DB-recorded video_path, then falls back to the conventional
    location <output_dir>/videos/clip_NN.<ext>. The fallback is what makes
    --backup reliable: an earlier redo clears video_path to NULL on reset,
    so without it a second redo of the same clip would back up nothing even
    though the previous MP4 is still on disk, about to be overwritten.
    """
    stored = clip.get("video_path")
    if stored:
        p = paths.resolve(stored)
        if p.exists():
            return p
    videos_dir = output_dir / "videos"
    for ext in _VIDEO_EXTS:
        cand = videos_dir / f"clip_{clip['clip_order']:02d}{ext}"
        if cand.exists():
            return cand
    return None


def _backup_videos(output_dir: Path, clips: list[dict], dry: bool) -> Path | None:
    """Copy each chosen clip's current video into videos/_backup/<stamp>/
    before the redo overwrites it.

    Inspects what is actually on disk (see _find_clip_video) rather than
    trusting the DB video_path alone, so clips whose state was already reset
    by an earlier redo still get backed up."""
    targets = [(c, p) for c in clips
               if (p := _find_clip_video(output_dir, c)) is not None]
    if not targets:
        log.info("nothing to back up (no existing MP4s for chosen clips)")
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_dir / "videos" / "_backup" / stamp
    log.info("%s %d MP4(s) → %s",
             "would back up" if dry else "backing up", len(targets), base)
    for c, src in targets:
        log.info("  clip %02d  ← %s", c["clip_order"], src)
    if dry:
        return base
    base.mkdir(parents=True, exist_ok=True)
    for _c, src in targets:
        shutil.copy2(src, base / src.name)
    return base


def _reset_video(conn, run_id: int, orders: set[int]) -> int:
    placeholders = ",".join("?" * len(orders))
    cur = conn.execute(
        f"UPDATE clips SET "
        f"  video_status='pending', video_path=NULL, comfyui_prompt_id=NULL, "
        f"  video_started_at=NULL, video_completed_at=NULL, "
        f"  video_duration_real_s=NULL, video_error=NULL, video_attempts=0 "
        f"WHERE run_id=? AND clip_order IN ({placeholders})",
        (run_id, *sorted(orders)),
    )
    return cur.rowcount


def _reset_image(conn, run_id: int, orders: set[int]) -> int:
    placeholders = ",".join("?" * len(orders))
    cur = conn.execute(
        f"UPDATE clips SET "
        f"  image_status='pending', image_path=NULL, image_cropped_path=NULL, "
        f"  image_seed=NULL, image_attempts=0, image_error=NULL, "
        f"  image_completed_at=NULL "
        f"WHERE run_id=? AND clip_order IN ({placeholders})",
        (run_id, *sorted(orders)),
    )
    return cur.rowcount


def _run(cmd: list[str]) -> None:
    log.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--clips", required=True,
                        help='Clip selector. Same syntax as stage4: '
                             '"7", "3,7,11", "5-10", "7-", "-3".')
    parser.add_argument("--scope", default="video",
                        help="What state to reset: video | image,video | all. "
                             "Default: video (keeps existing image, regenerates video only).")
    parser.add_argument("--backup", action="store_true",
                        help="Copy existing MP4s to a timestamped _backup/ folder "
                             "before reset, so stage5 doesn't overwrite them in place.")
    parser.add_argument("--no-requeue", action="store_true",
                        help="Reset state only; don't run stage2/3/4 afterwards.")
    parser.add_argument("--notify", action="store_true",
                        help="Pass --notify to downstream stage scripts.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen, write nothing.")
    args = parser.parse_args()

    orders = _parse_clip_filter(args.clips)
    if not orders:
        raise SystemExit("--clips resolved to empty set")
    scope = _parse_scope(args.scope)

    matched = _select_clips(args.run_id, orders)
    if not matched:
        raise SystemExit(f"no clips matched run_id={args.run_id} clips={sorted(orders)}")

    found = [c["clip_order"] for c in matched]
    missing = sorted(orders - set(found))
    log.info("run_id=%d  scope=%s  clips matched=%s%s",
             args.run_id, sorted(scope), found,
             f"  (ignored, not in run: {missing})" if missing else "")

    if args.backup:
        _backup_videos(_output_dir(args.run_id), matched, args.dry_run)

    if args.dry_run:
        log.info("[dry-run] would reset %s state for clips %s",
                 sorted(scope), found)
        if not args.no_requeue:
            chain = []
            if "image" in scope:
                chain += ["stage2_scene_images", "stage3_crop_watermark"]
            chain.append(f"stage4_queue_videos --clips {','.join(map(str, found))}")
            log.info("[dry-run] would then run: %s", " → ".join(chain))
        return 0

    with db.connect() as conn:
        if "image" in scope:
            n = _reset_image(conn, args.run_id, set(found))
            log.info("✓ reset image state on %d clip(s)", n)
        if "video" in scope:
            n = _reset_video(conn, args.run_id, set(found))
            log.info("✓ reset video state on %d clip(s)", n)

    event(
        "redo",
        f"reset scope={sorted(scope)} on clips {found}",
        run_id=args.run_id,
        payload={"clips": found, "scope": sorted(scope), "backup": args.backup},
    )

    if args.no_requeue:
        log.info("--no-requeue set, stopping after reset")
        if "image" in scope:
            log.info("(image was reset — when ready, run stage2 + stage3 before stage4)")
        return 0

    # Chain stages. If image was reset, regenerate it (and crop) before queueing.
    if "image" in scope:
        cmd = [PY, "scripts/stage2_scene_images.py", "--run-id", str(args.run_id)]
        if args.notify:
            cmd.append("--notify")
        _run(cmd)
        _run([PY, "scripts/stage3_crop_watermark.py", "--run-id", str(args.run_id)])

    clips_csv = ",".join(str(o) for o in found)
    cmd = [PY, "scripts/stage4_queue_videos.py",
           "--run-id", str(args.run_id), "--clips", clips_csv]
    if args.notify:
        cmd.append("--notify")
    _run(cmd)

    log.info("done. Make sure stage5_poll_videos.py --run-id %d is running so "
             "the new MP4s get picked up.", args.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
