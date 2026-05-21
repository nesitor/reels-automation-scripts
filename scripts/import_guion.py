"""Import a versioned guion JSON into the state DB and create a run row.

Usage:
    python scripts/import_guion.py guion/video2.v1.json --profile preview
    python scripts/import_guion.py guion/video2.v1.json --profile final --notes "second pass"

Idempotency:
  - (video_id, version) is unique. Re-importing the same JSON reuses the
    existing guiones row.
  - A fresh `runs` row is created on every invocation, so you can do many
    preview/final passes per guion.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, paths
from lib.config import CFG
from lib.logger import event, get

log = get("import_guion")


def _upsert_guion(conn, payload: dict, json_path: Path) -> int:
    row = conn.execute(
        "SELECT id FROM guiones WHERE video_id=? AND version=?",
        (payload["video_id"], payload["version"]),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO guiones(video_id, version, title, json_path, duration_target_s, notes) "
        "VALUES (?,?,?,?,?,?)",
        (
            payload["video_id"],
            payload["version"],
            payload.get("title"),
            paths.rel(json_path),
            payload.get("duration_target_s"),
            payload.get("notes"),
        ),
    )
    return cur.lastrowid


def _upsert_protagonist(conn, guion_id: int, prota: dict) -> None:
    """Pre-seed the protagonist row at variation_index=0 from the guion."""
    exists = conn.execute(
        "SELECT 1 FROM protagonists WHERE guion_id=? AND variation_index=0",
        (guion_id,),
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO protagonists(guion_id, variation_index, prompt) VALUES (?,?,?)",
        (guion_id, 0, prota["prompt"]),
    )


def _create_run(conn, guion_id: int, profile: str, output_dir: Path, notes: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO runs(guion_id, profile, output_dir, notes) VALUES (?,?,?,?)",
        (guion_id, profile, paths.rel(output_dir), notes),
    )
    return cur.lastrowid


def _insert_clips(conn, run_id: int, clips: list[dict], default_neg: str | None) -> None:
    for clip in clips:
        neg = clip.get("video_prompt_negative") or default_neg
        use_ref = 1 if clip.get("use_protagonist_reference", True) else 0
        conn.execute(
            "INSERT INTO clips("
            " run_id, clip_order, section, beat, dialogue, scene_summary, "
            " image_prompt, video_prompt, video_prompt_negative, use_protagonist_reference"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                clip["order"],
                clip.get("section"),
                clip.get("beat"),
                clip.get("dialogue"),
                clip.get("scene_summary"),
                clip["image_prompt"],
                clip["video_prompt"],
                neg,
                use_ref,
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("guion_json", type=Path)
    parser.add_argument("--profile", choices=["preview", "final"], default="preview")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args()

    # Resolve against the CWD now so the stored relative path is correct
    # regardless of where the command was launched from.
    json_path: Path = args.guion_json.resolve()
    if not json_path.exists():
        log.error("guion file not found: %s", json_path)
        return 2

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    db.init_schema()

    output_dir = CFG.outputs_dir / payload["video_id"] / f"v{payload['version']}_{args.profile}"
    output_dir.mkdir(parents=True, exist_ok=True)

    defaults = payload.get("defaults", {})
    default_neg = defaults.get("video_negative_prompt")

    with db.connect() as conn:
        guion_id = _upsert_guion(conn, payload, json_path)
        _upsert_protagonist(conn, guion_id, payload["protagonist"])
        run_id = _create_run(conn, guion_id, args.profile, output_dir, args.notes)
        _insert_clips(conn, run_id, payload["clips"], default_neg)

    event(
        "import_guion",
        f"imported {payload['video_id']} v{payload['version']} → run_id={run_id} profile={args.profile}",
        run_id=run_id,
    )
    print(f"run_id={run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
