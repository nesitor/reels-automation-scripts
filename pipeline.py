"""High-level orchestrator. Calls a chosen subset of stages 2 → 6.

Stage 1 (protagonist generation + approval) is NOT auto-run because it needs
a human eye to pick the variation. Run it once manually:

    python scripts/stage1_protagonist.py --video-id video2_decisiones_sin_conocerte --variations 6
    python scripts/stage1_protagonist.py --video-id video2_decisiones_sin_conocerte --approve 3

Common invocations:

    # Default: import guion, then stages 2, 3, 4 (submit-and-detach)
    python pipeline.py --guion guion/video2.v1.json --profile preview --notify

    # Resume an existing run; only run stages 3 and 4
    python pipeline.py --run-id 1 --stages 3,4 --notify

    # Resume and run from stage 4 to the end (incl. polling + compile)
    python pipeline.py --run-id 1 --from-stage 4 --watch --compile --notify

    # Skip stage 2 (because you already have images, see adopt_assets.py)
    python pipeline.py --run-id 1 --skip-stages 2 --notify

Stages reference:
    2  generate clip images with Nano Banana 2
    3  crop watermark from each image
    4  submit clips to ComfyUI queue (fast)
    5  poll ComfyUI until every clip is done (LONG: 45 min/clip)
    6  ffmpeg-concat finished clips into preview.mp4

Every stage is idempotent; re-running this command picks up where the last
run stopped, because state lives in SQLite.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import db
from lib.logger import get

log = get("pipeline")
ROOT = Path(__file__).resolve().parent
PY = sys.executable

# Mapping: stage number → (script name, accepts --notify?)
STAGES: dict[int, tuple[str, bool]] = {
    2: ("scripts/stage2_scene_images.py", True),
    3: ("scripts/stage3_crop_watermark.py", False),
    4: ("scripts/stage4_queue_videos.py", True),
    5: ("scripts/stage5_poll_videos.py", True),
    6: ("scripts/stage6_compile.py", True),
}

DEFAULT_STAGES = {2, 3, 4}  # safe submit-and-detach default


def _run(cmd: list[str]) -> None:
    log.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def _import_guion(guion: Path, profile: str, notes: str | None) -> int:
    cmd = [PY, "scripts/import_guion.py", str(guion), "--profile", profile]
    if notes:
        cmd += ["--notes", notes]
    log.info("$ %s", " ".join(cmd))
    out = subprocess.run(cmd, check=True, cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr)
    for line in out.stdout.splitlines():
        if line.startswith("run_id="):
            return int(line.split("=", 1)[1])
    raise RuntimeError("import_guion did not print run_id=...")


def _parse_int_list(s: str | None) -> set[int]:
    return {int(x) for x in (s or "").split(",") if x.strip()}


def _resolve_stages(args: argparse.Namespace) -> list[int]:
    """Decide which stages will execute, applying flags in priority order:

      --stages X,Y     → exact set (highest priority)
      --from-stage N   → include N..(to-stage or 6)
      --to-stage N     → include (from-stage or 2)..N
      otherwise        → DEFAULT_STAGES = {2,3,4}

    Then:
      --watch          → add 5
      --compile        → add 6
      --skip-stages    → remove
    """
    if args.stages:
        selected = _parse_int_list(args.stages)
    elif args.from_stage is not None or args.to_stage is not None:
        lo = args.from_stage if args.from_stage is not None else 2
        hi = args.to_stage if args.to_stage is not None else 6
        selected = {n for n in STAGES if lo <= n <= hi}
    else:
        selected = set(DEFAULT_STAGES)

    if args.watch:
        selected.add(5)
    if args.compile:
        selected.add(6)
    for n in _parse_int_list(args.skip_stages):
        selected.discard(n)

    unknown = selected - set(STAGES)
    if unknown:
        raise SystemExit(f"unknown stage numbers: {sorted(unknown)}")
    return sorted(selected)


def _run_stage(stage_num: int, run_id: int, notify: bool) -> None:
    script, accepts_notify = STAGES[stage_num]
    cmd = [PY, script, "--run-id", str(run_id)]
    if accepts_notify and notify:
        cmd.append("--notify")
    _run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--guion", type=Path, default=None,
                        help="Import this guion as a new run.")
    target.add_argument("--run-id", type=int, default=None,
                        help="Reuse an existing run (skips import).")

    parser.add_argument("--profile", choices=["preview", "final"], default="preview")
    parser.add_argument("--notes", default=None)

    parser.add_argument("--stages", default=None,
                        help="Exact comma-separated set of stages to run (e.g. 3,4). Overrides --from/--to.")
    parser.add_argument("--from-stage", type=int, default=None,
                        help="Start at this stage (inclusive).")
    parser.add_argument("--to-stage", type=int, default=None,
                        help="Stop at this stage (inclusive).")
    parser.add_argument("--skip-stages", default="",
                        help="Comma-separated list of stages to skip.")

    parser.add_argument("--watch", action="store_true",
                        help="Include stage 5 (poll ComfyUI until done).")
    parser.add_argument("--compile", action="store_true",
                        help="Include stage 6 (compile preview.mp4).")
    parser.add_argument("--notify", action="store_true")

    args = parser.parse_args()
    db.init_schema()

    if args.run_id:
        run_id = args.run_id
        log.info("resuming run_id=%d", run_id)
    else:
        run_id = _import_guion(args.guion, args.profile, args.notes)
        log.info("→ new run_id=%d", run_id)

    stages = _resolve_stages(args)
    if not stages:
        log.warning("no stages selected, nothing to do")
        return 0
    log.info("running stages: %s on run_id=%d", stages, run_id)

    for n in stages:
        _run_stage(n, run_id, args.notify)

    if 5 not in stages and 4 in stages:
        log.info("stage 5 not selected — launch the poller yourself when ready:")
        log.info("  nohup python scripts/stage5_poll_videos.py --run-id %d --notify &> stage5.log &",
                 run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
