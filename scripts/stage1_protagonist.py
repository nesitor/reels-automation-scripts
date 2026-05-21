"""Stage 1 — generate protagonist reference image(s).

Generates N variations of the same prompt with different seeds so you can
pick the one you like. After visual review, run:

    python scripts/stage1_protagonist.py --approve <variation_index>

to mark which variation is the canonical face reference. From that point on
stage2 will pass that image to every clip image generation.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, nano_banana, paths
from lib.config import CFG
from lib.logger import event, get

log = get("stage1")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_guion_id(args: argparse.Namespace) -> int:
    with db.connect(readonly=True) as conn:
        if args.guion_id:
            return args.guion_id
        row = conn.execute(
            "SELECT id FROM guiones WHERE video_id=? ORDER BY version DESC LIMIT 1",
            (args.video_id,),
        ).fetchone()
        if not row:
            raise SystemExit(f"no guion found for video_id={args.video_id!r}")
        return row["id"]


def _generate_variations(guion_id: int, n: int) -> None:
    with db.connect(readonly=True) as conn:
        base = conn.execute(
            "SELECT prompt FROM protagonists WHERE guion_id=? AND variation_index=0",
            (guion_id,),
        ).fetchone()
    if not base:
        raise SystemExit("protagonist row not seeded; re-run import_guion first")

    out_dir = CFG.outputs_dir / "protagonist" / f"guion_{guion_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n):
        with db.connect() as conn:
            existing = conn.execute(
                "SELECT status, reference_image_path FROM protagonists "
                "WHERE guion_id=? AND variation_index=?",
                (guion_id, i),
            ).fetchone()
            if existing and existing["status"] == "done":
                log.info("variation %d already done, skip", i)
                continue
            if not existing:
                conn.execute(
                    "INSERT INTO protagonists(guion_id, variation_index, prompt, status) "
                    "VALUES (?,?,?,?)",
                    (guion_id, i, base["prompt"], "generating"),
                )
            else:
                conn.execute(
                    "UPDATE protagonists SET status='generating' "
                    "WHERE guion_id=? AND variation_index=?",
                    (guion_id, i),
                )

        seed = random.randint(1, 2**31 - 1)
        out_path = out_dir / f"variation_{i:02d}_seed{seed}.png"
        try:
            nano_banana.generate_image(base["prompt"], out_path=out_path, seed=seed)
        except Exception as exc:  # noqa: BLE001
            with db.connect() as conn:
                conn.execute(
                    "UPDATE protagonists SET status='failed', error=? "
                    "WHERE guion_id=? AND variation_index=?",
                    (str(exc), guion_id, i),
                )
            event("stage1", f"variation {i} failed: {exc}", level="error")
            continue

        with db.connect() as conn:
            conn.execute(
                "UPDATE protagonists SET status='done', reference_image_path=?, seed=?, "
                "completed_at=? WHERE guion_id=? AND variation_index=?",
                (paths.rel(out_path), seed, _now(), guion_id, i),
            )
        event("stage1", f"variation {i} done → {out_path.name}")


def _approve(guion_id: int, variation_index: int) -> None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT reference_image_path FROM protagonists "
            "WHERE guion_id=? AND variation_index=? AND status='done'",
            (guion_id, variation_index),
        ).fetchone()
        if not row:
            raise SystemExit(
                f"variation {variation_index} not in 'done' state. Generate first."
            )
        conn.execute(
            "UPDATE protagonists SET approved=0, approved_at=NULL WHERE guion_id=?",
            (guion_id,),
        )
        conn.execute(
            "UPDATE protagonists SET approved=1, approved_at=? "
            "WHERE guion_id=? AND variation_index=?",
            (_now(), guion_id, variation_index),
        )
    event("stage1", f"approved variation {variation_index} (path={row['reference_image_path']})")
    print(row["reference_image_path"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", default=None)
    parser.add_argument("--guion-id", type=int, default=None)
    parser.add_argument("--variations", type=int, default=6)
    parser.add_argument("--approve", type=int, default=None,
                        help="Mark the given variation_index as approved.")
    args = parser.parse_args()
    if not args.video_id and not args.guion_id:
        parser.error("specify --video-id or --guion-id")

    guion_id = _resolve_guion_id(args)
    if args.approve is not None:
        _approve(guion_id, args.approve)
        return 0
    _generate_variations(guion_id, args.variations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
