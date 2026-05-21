"""Print every node in a ComfyUI workflow JSON (API format) so you can fill
node_map.json without opening the file by hand.

Usage:
    python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json

By default it shows id, class_type, title (if present) and a short hint of
the inputs that matter for our pipeline (text, image, seed, filename_prefix).
Use --full to dump every input of every node.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

console = Console()

# Inputs we display in the "hint" column. Other inputs are hidden unless --full.
INTERESTING_KEYS = (
    "text", "image", "seed", "noise_seed",
    "filename_prefix", "ckpt_name", "lora_name",
    "width", "height", "length", "frame_rate", "steps", "cfg",
)


def _short(value, limit: int = 70) -> str:
    if isinstance(value, list):
        # references to another node: [node_id, output_slot]
        return f"→ node {value[0]}[{value[1]}]"
    s = str(value).replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--full", action="store_true",
                        help="Show all inputs of every node.")
    parser.add_argument("--filter", default=None,
                        help="Only show nodes whose class_type contains this substring.")
    args = parser.parse_args()

    if not args.workflow.exists():
        console.print(f"[red]not found: {args.workflow}[/]")
        return 2

    wf = json.loads(args.workflow.read_text(encoding="utf-8"))
    console.print(f"[bold]{len(wf)} nodes in {args.workflow.name}[/]\n")

    table = Table(expand=True, show_lines=False)
    table.add_column("id", style="cyan", justify="right")
    table.add_column("class_type", style="magenta")
    table.add_column("title", style="dim")
    table.add_column("inputs (matching keys)" if not args.full else "inputs")

    def sort_key(item):
        nid = item[0]
        return int(nid) if nid.isdigit() else 1 << 30

    for node_id, node in sorted(wf.items(), key=sort_key):
        ct = node.get("class_type", "?")
        if args.filter and args.filter.lower() not in ct.lower():
            continue
        title = node.get("_meta", {}).get("title", "") or ""
        inputs = node.get("inputs", {}) or {}
        keys = inputs.keys() if args.full else [k for k in inputs if k in INTERESTING_KEYS]
        hint_lines = [f"[green]{k}[/]={_short(inputs[k])}" for k in keys]
        table.add_row(node_id, ct, title, "  ".join(hint_lines))

    console.print(table)
    console.print(
        "\n[bold]How to fill node_map.json:[/]\n"
        "  • [cyan]prompt_positive[/]  → a CLIPTextEncode whose `text` is your positive prompt\n"
        "  • [cyan]prompt_negative[/]  → a CLIPTextEncode whose `text` is your negative prompt (optional)\n"
        "  • [cyan]input_image[/]      → a LoadImage / LTXVImageToVideo source\n"
        "  • [cyan]seed[/]             → a sampler node; check whether its key is `seed` or `noise_seed`\n"
        "  • [cyan]output_filename[/]  → a SaveVideo / VHS_VideoCombine; key is usually `filename_prefix`\n"
        "\nFor unusual input keys use the explicit form:\n"
        '  "seed": {"node_id": "25", "input": "noise_seed"}\n'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
