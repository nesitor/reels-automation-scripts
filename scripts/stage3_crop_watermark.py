"""Stage 3 — crop the Nano Banana 2 watermark from every clip image.

Idempotent: only processes clips with image_status='done' that don't yet have
an image_cropped_path. Re-running is a no-op once done.

Usage:
    python scripts/stage3_crop_watermark.py --run-id 1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, image_utils, paths
from lib.config import CFG
from lib.logger import event, get

log = get("stage3")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--crop-px", type=int, default=None,
                        help="Override WATERMARK_CROP_PX for this run.")
    args = parser.parse_args()

    with db.connect(readonly=True) as conn:
        out_dir_row = conn.execute(
            "SELECT output_dir FROM runs WHERE id=?", (args.run_id,)
        ).fetchone()
        if not out_dir_row:
            raise SystemExit(f"run_id {args.run_id} not found")
        clips = conn.execute(
            "SELECT id, clip_order, image_path FROM clips "
            "WHERE run_id=? AND image_status='done' AND "
            "(image_cropped_path IS NULL OR image_cropped_path = '') "
            "ORDER BY clip_order",
            (args.run_id,),
        ).fetchall()

    cropped_dir = paths.resolve(out_dir_row["output_dir"]) / "images_cropped"
    cropped_dir.mkdir(parents=True, exist_ok=True)

    log.info("cropping %d images (crop_px=%s)",
             len(clips), args.crop_px if args.crop_px is not None else CFG.watermark_crop_px)
    for c in clips:
        src = paths.resolve(c["image_path"])
        dst = cropped_dir / f"clip_{c['clip_order']:02d}_cropped.png"
        try:
            image_utils.crop_watermark(src, dst, crop_px=args.crop_px)
        except Exception as exc:  # noqa: BLE001
            event("stage3", f"crop failed clip {c['clip_order']}: {exc}",
                  run_id=args.run_id, clip_id=c["id"], level="error")
            continue
        with db.connect() as conn:
            conn.execute(
                "UPDATE clips SET image_cropped_path=? WHERE id=?",
                (paths.rel(dst), c["id"]),
            )
        event("stage3", f"clip {c['clip_order']} cropped",
              run_id=args.run_id, clip_id=c["id"])

    log.info("stage3 done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
