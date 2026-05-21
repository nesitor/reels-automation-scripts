"""Stage 6 — concatenate finished clip MP4s into a single preview file.

Uses ffmpeg's concat demuxer (lossless when codecs match). Only runs if every
clip in the run has video_status='done'.

Usage:
    python scripts/stage6_compile.py --run-id 1
    python scripts/stage6_compile.py --run-id 1 --output preview.mp4
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, paths, telegram
from lib.logger import event, get

log = get("stage6")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--allow-partial", action="store_true",
                        help="Compile even if some clips are not done.")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        log.error("ffmpeg not found in PATH. Install it (brew install ffmpeg).")
        return 2

    with db.connect(readonly=True) as conn:
        run = conn.execute("SELECT output_dir FROM runs WHERE id=?", (args.run_id,)).fetchone()
        if not run:
            log.error("run %d not found", args.run_id)
            return 2
        clips = conn.execute(
            "SELECT clip_order, video_status, video_path FROM clips "
            "WHERE run_id=? ORDER BY clip_order",
            (args.run_id,),
        ).fetchall()

    not_done = [c["clip_order"] for c in clips if c["video_status"] != "done"]
    if not_done and not args.allow_partial:
        log.error("clips not done: %s. Use --allow-partial to compile anyway.", not_done)
        return 3

    video_files = [paths.resolve(c["video_path"]) for c in clips
                   if c["video_status"] == "done" and c["video_path"]]
    if not video_files:
        log.error("no finished clips found")
        return 3

    out = args.output or (paths.resolve(run["output_dir"]) / "preview.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        for p in video_files:
            fh.write(f"file '{p.resolve()}'\n")
        list_path = Path(fh.name)

    try:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", str(out),
        ]
        log.info("running: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
    finally:
        list_path.unlink(missing_ok=True)

    event("stage6", f"compiled {len(video_files)} clips → {out}", run_id=args.run_id)
    log.info("→ %s", out)
    if args.notify:
        telegram.send(
            ("preview_ready", str(args.run_id)),
            f"🎞️ *Preview ready* — run `{args.run_id}`\n`{out}`",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
