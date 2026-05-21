"""Stage 2 — generate the first-frame image for each clip using Nano Banana 2.

Idempotent: only processes clips where image_status IN ('pending','failed') AND
attempts < IMAGE_MAX_ATTEMPTS. Passes the approved protagonist image as
multimodal context so the face stays consistent across the 16 clips.

Usage:
    python scripts/stage2_scene_images.py --run-id 1
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, nano_banana, paths, telegram
from lib.config import CFG
from lib.logger import event, get

log = get("stage2")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_meta(run_id: int) -> tuple[int, Path]:
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT guion_id, output_dir FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"run_id {run_id} not found")
    return row["guion_id"], paths.resolve(row["output_dir"])


def _approved_protagonist(guion_id: int) -> Path:
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT reference_image_path FROM protagonists "
            "WHERE guion_id=? AND approved=1 AND status='done'",
            (guion_id,),
        ).fetchone()
    if not row:
        raise SystemExit(
            "no approved protagonist for this guion. Run stage1_protagonist.py "
            "with --approve <variation_index> first."
        )
    return paths.resolve(row["reference_image_path"])


def _pending_clips(run_id: int) -> list[dict]:
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, clip_order, image_prompt, image_attempts, "
            "use_protagonist_reference "
            "FROM clips WHERE run_id=? "
            "AND image_status IN ('pending','failed') "
            "AND image_attempts < ? "
            "ORDER BY clip_order",
            (run_id, CFG.image_max_attempts),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark(clip_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), clip_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--notify", action="store_true",
                        help="Send a Telegram ping when this stage finishes.")
    args = parser.parse_args()

    guion_id, out_dir = _run_meta(args.run_id)
    ref_path = _approved_protagonist(guion_id)
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    pending = _pending_clips(args.run_id)
    if not pending:
        log.info("nothing to do, all clip images are done or maxed out")
        return 0
    log.info("processing %d pending clip images using ref=%s", len(pending), ref_path.name)

    n_ok, n_fail = 0, 0
    for clip in pending:
        clip_id = clip["id"]
        order = clip["clip_order"]
        _mark(clip_id, image_status="generating", image_attempts=clip["image_attempts"] + 1)
        seed = random.randint(1, 2**31 - 1)
        out_path = images_dir / f"clip_{order:02d}.png"
        # Skip protagonist face-ref for b-roll and clips with non-protagonist
        # speakers so the model doesn't bleed her face into other characters.
        refs = [ref_path] if clip.get("use_protagonist_reference", 1) else []
        try:
            nano_banana.generate_image(
                clip["image_prompt"],
                out_path=out_path,
                reference_images=refs,
                seed=seed,
            )
            _mark(
                clip_id,
                image_status="done",
                image_path=paths.rel(out_path),
                image_seed=seed,
                image_error=None,
                image_completed_at=_now(),
            )
            event("stage2", f"clip {order} image done", run_id=args.run_id, clip_id=clip_id)
            n_ok += 1
        except Exception as exc:  # noqa: BLE001
            _mark(clip_id, image_status="failed", image_error=str(exc))
            event(
                "stage2",
                f"clip {order} image failed: {exc}",
                run_id=args.run_id, clip_id=clip_id, level="error",
            )
            n_fail += 1

    log.info("stage2 done: ok=%d failed=%d", n_ok, n_fail)
    if args.notify:
        telegram.send(
            ("stage2_done", str(args.run_id), str(n_ok), str(n_fail)),
            f"🖼️ *Stage 2 complete* — run `{args.run_id}`\n"
            f"images: ✅ {n_ok}  ❌ {n_fail}",
        )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
