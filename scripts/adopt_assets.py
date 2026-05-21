"""Adopt pre-existing image/video files into the SQLite state.

Useful when you generated some assets manually outside the pipeline (e.g.
images from a prior session, or video clips you rendered directly in
ComfyUI) and want subsequent stages to treat them as `done` and skip them.

Where files end up:
  Assets that already live inside the project tree are registered in place
  (the DB stores their project-relative path). Assets from OUTSIDE the
  project are first copied into `imported_assets/<run_id>/<category>/` —
  category being images / images_cropped / videos — so every path the DB
  records stays relative to the project root and survives a folder move.
  The original external file is left untouched.

File-name convention:
  Each filename must contain the substring `clip` followed by a number that
  matches `clip_order` in the DB. The match is case-insensitive and tolerates
  underscores or hyphens between `clip` and the number. Examples that work:
    clip_01.png    clip-3.mp4    Aspectados_clip07_final_v2.png
  Files that don't match the pattern are skipped (with a log line).

Usage:
    # Raw NB2 images (still need cropping in stage3)
    python scripts/adopt_assets.py --run-id 1 \\
        --images-raw outputs/video2/v1_preview/images

    # Already-cropped images (stage3 will skip them)
    python scripts/adopt_assets.py --run-id 1 \\
        --images-cropped outputs/video2/v1_preview/images_cropped

    # Finished MP4 clips (stages 4 + 5 will skip them)
    python scripts/adopt_assets.py --run-id 1 \\
        --videos outputs/video2/v1_preview/videos

    # Auto-discover all three under the run's output_dir:
    python scripts/adopt_assets.py --run-id 1 --auto

    # Dry-run: show what would be adopted without writing to the DB:
    python scripts/adopt_assets.py --run-id 1 --auto --dry-run

Idempotent: re-running adopts the same files again with no further DB change.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, paths
from lib.logger import event, get

log = get("adopt")

# Matches "clip" + optional separator (underscore, hyphen, or whitespace) + integer.
# Case-insensitive. Examples that match: clip_01, clip-3, "clip 4", clip07.
_CLIP_RE = re.compile(r"clip[\s_\-]*(\d+)", re.IGNORECASE)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".gif", ".mkv")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scan(directory: Path, exts: tuple[str, ...]) -> dict[int, Path]:
    """Return {clip_order: path} for files in `directory` whose name encodes
    a clip number and whose extension is in `exts`. First match per clip wins;
    duplicates are warned about and ignored."""
    mapping: dict[int, Path] = {}
    if not directory.is_dir():
        log.warning("not a directory, skipping: %s", directory)
        return mapping
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        m = _CLIP_RE.search(p.stem)
        if not m:
            log.debug("no clip-number in name, skip: %s", p.name)
            continue
        order = int(m.group(1))
        if order in mapping:
            log.warning("duplicate file for clip %02d: kept %s, ignored %s",
                        order, mapping[order].name, p.name)
            continue
        mapping[order] = p
    return mapping


def _valid_clip_orders(conn, run_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT clip_order FROM clips WHERE run_id=?", (run_id,)
    ).fetchall()
    return {r["clip_order"] for r in rows}


def _localize(run_id: int, category: str, suffix: str,
              order: int, src: Path, dry: bool) -> Path:
    """Return an in-tree absolute path for `src`.

    Assets already inside the project tree keep their location. Assets from
    outside are copied into `imported_assets/<run_id>/<category>/` under the
    canonical clip name, so the DB only ever stores project-relative paths
    and a folder move never breaks them.
    """
    src = src.resolve()
    if paths.is_inside(src):
        return src
    dest = (paths.IMPORTED_ASSETS / str(run_id) / category
            / f"clip_{order:02d}{suffix}{src.suffix.lower()}")
    log.info("    external asset → copying into %s", paths.rel(dest))
    if not dry:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return dest


def _adopt_images_raw(conn, run_id: int, mapping: dict[int, Path], dry: bool) -> int:
    valid = _valid_clip_orders(conn, run_id)
    n = 0
    for order, path in sorted(mapping.items()):
        if order not in valid:
            log.warning("clip_order %d not in run; ignoring %s", order, path.name)
            continue
        log.info("[raw image] clip %02d ← %s", order, path)
        local = _localize(run_id, "images", "", order, path, dry)
        if dry:
            n += 1
            continue
        cur = conn.execute(
            "UPDATE clips SET image_status='done', image_path=?, "
            "image_completed_at=COALESCE(image_completed_at, ?), image_error=NULL "
            "WHERE run_id=? AND clip_order=?",
            (paths.rel(local), _now(), run_id, order),
        )
        n += cur.rowcount
    return n


def _adopt_images_cropped(conn, run_id: int, mapping: dict[int, Path], dry: bool) -> int:
    valid = _valid_clip_orders(conn, run_id)
    n = 0
    for order, path in sorted(mapping.items()):
        if order not in valid:
            log.warning("clip_order %d not in run; ignoring %s", order, path.name)
            continue
        log.info("[cropped image] clip %02d ← %s", order, path)
        local = _localize(run_id, "images_cropped", "_cropped", order, path, dry)
        if dry:
            n += 1
            continue
        # If image_path is empty we also point it at the cropped file so the
        # invariant "image_status=done ⇒ image_path is not null" holds.
        rel = paths.rel(local)
        cur = conn.execute(
            "UPDATE clips SET image_cropped_path=?, image_status='done', "
            "image_path=COALESCE(image_path, ?), "
            "image_completed_at=COALESCE(image_completed_at, ?), image_error=NULL "
            "WHERE run_id=? AND clip_order=?",
            (rel, rel, _now(), run_id, order),
        )
        n += cur.rowcount
    return n


def _adopt_videos(conn, run_id: int, mapping: dict[int, Path], dry: bool) -> int:
    valid = _valid_clip_orders(conn, run_id)
    n = 0
    for order, path in sorted(mapping.items()):
        if order not in valid:
            log.warning("clip_order %d not in run; ignoring %s", order, path.name)
            continue
        log.info("[video] clip %02d ← %s", order, path)
        local = _localize(run_id, "videos", "", order, path, dry)
        if dry:
            n += 1
            continue
        cur = conn.execute(
            "UPDATE clips SET video_status='done', video_path=?, "
            "video_completed_at=COALESCE(video_completed_at, ?), video_error=NULL "
            "WHERE run_id=? AND clip_order=?",
            (paths.rel(local), _now(), run_id, order),
        )
        n += cur.rowcount
    return n


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--images-raw", type=Path, default=None,
                        help="Directory of raw NB2 images (will still go through stage3).")
    parser.add_argument("--images-cropped", type=Path, default=None,
                        help="Directory of already-cropped images (stage3 will skip).")
    parser.add_argument("--videos", type=Path, default=None,
                        help="Directory of finished MP4s (stages 4+5 will skip).")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-discover images/, images_cropped/, videos/ inside the run's output_dir.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without touching the DB.")
    args = parser.parse_args()

    with db.connect(readonly=True) as conn:
        row = conn.execute("SELECT output_dir FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not row:
        raise SystemExit(f"run_id {args.run_id} not found")
    out_dir = paths.resolve(row["output_dir"])

    if args.auto:
        args.images_raw     = args.images_raw     or (out_dir / "images")
        args.images_cropped = args.images_cropped or (out_dir / "images_cropped")
        args.videos         = args.videos         or (out_dir / "videos")

    # Resolve any explicitly-passed dirs (CWD-relative) to absolute so the
    # file scan and the in-tree/external check operate on the same paths.
    for attr in ("images_raw", "images_cropped", "videos"):
        d = getattr(args, attr)
        if d is not None:
            setattr(args, attr, Path(d).resolve())

    if not any((args.images_raw, args.images_cropped, args.videos)):
        parser.error("nothing to adopt — pass --images-raw / --images-cropped / --videos / --auto")

    totals = {"images_raw": 0, "images_cropped": 0, "videos": 0}
    with db.connect() as conn:
        if args.images_raw:
            mapping = _scan(args.images_raw, IMAGE_EXTS)
            totals["images_raw"] = _adopt_images_raw(conn, args.run_id, mapping, args.dry_run)
        if args.images_cropped:
            mapping = _scan(args.images_cropped, IMAGE_EXTS)
            totals["images_cropped"] = _adopt_images_cropped(conn, args.run_id, mapping, args.dry_run)
        if args.videos:
            mapping = _scan(args.videos, VIDEO_EXTS)
            totals["videos"] = _adopt_videos(conn, args.run_id, mapping, args.dry_run)

    if args.dry_run:
        log.info("[dry-run] would adopt: %s", totals)
    else:
        event("adopt", f"adopted {totals}", run_id=args.run_id, payload=totals)
        log.info("done: %s", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
