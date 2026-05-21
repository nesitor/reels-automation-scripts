"""Pretty status dashboard for a run. Read-only against the SQLite state DB.

Usage:
    python scripts/status.py                # latest run
    python scripts/status.py --run-id 1
    python scripts/status.py --watch         # refresh every 5s
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.live import Live
from rich.table import Table

from lib import db

console = Console()


def _latest_run() -> int | None:
    with db.connect(readonly=True) as conn:
        row = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def render(run_id: int) -> Table:
    with db.connect(readonly=True) as conn:
        run = conn.execute(
            "SELECT r.*, g.video_id, g.version, g.title "
            "FROM runs r JOIN guiones g ON g.id=r.guion_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        clips = conn.execute(
            "SELECT clip_order, section, beat, image_status, image_attempts, "
            "video_status, video_attempts, comfyui_prompt_id, video_path "
            "FROM clips WHERE run_id=? ORDER BY clip_order",
            (run_id,),
        ).fetchall()

    title = (f"Run {run_id} — {run['video_id']} v{run['version']} ({run['profile']})  "
             f"·  {run['title'] or ''}")
    t = Table(title=title, expand=True)
    t.add_column("#", justify="right", style="cyan")
    t.add_column("section", style="magenta")
    t.add_column("beat", justify="center")
    t.add_column("image", justify="center")
    t.add_column("video", justify="center")
    t.add_column("prompt_id", overflow="fold", style="dim")
    t.add_column("video_path", overflow="fold", style="green")

    color = {"pending": "yellow", "generating": "cyan", "queued": "blue",
             "running": "cyan", "done": "green", "failed": "red"}
    for c in clips:
        img_state = c["image_status"]
        vid_state = c["video_status"]
        t.add_row(
            f"{c['clip_order']:02d}",
            c["section"] or "",
            c["beat"] or "",
            f"[{color.get(img_state, 'white')}]{img_state}[/] ({c['image_attempts']})",
            f"[{color.get(vid_state, 'white')}]{vid_state}[/] ({c['video_attempts']})",
            (c["comfyui_prompt_id"] or "")[:18],
            Path(c["video_path"]).name if c["video_path"] else "",
        )
    return t


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or _latest_run()
    if not run_id:
        console.print("[red]no runs in DB[/]")
        return 1

    if not args.watch:
        console.print(render(run_id))
        return 0

    with Live(render(run_id), console=console, refresh_per_second=2) as live:
        while True:
            time.sleep(5)
            live.update(render(run_id))


if __name__ == "__main__":
    raise SystemExit(main())
