"""ComfyUI HTTP API client — works for local and remote ComfyUI alike.

Everything goes over HTTP. No shared filesystem assumption:

  - Input images are uploaded to <host>/upload/image (multipart POST).
  - Output videos are downloaded from <host>/view (streaming GET).

That means COMFYUI_HOST can point at any machine reachable by HTTP and the
pipeline works the same — no NFS, no SSHFS, no port forwarding for the
filesystem. Only the HTTP port needs to be open.

Why not an MCP for the pipeline: long-running jobs (45 min/clip) need a
state machine that survives Claude Code sessions and runs as a daemon.
The HTTP API is rock-solid for that pattern. The MCP can still be plugged
in separately for ad-hoc inspection (see docs/COMFYUI_MCP_OPTIONAL.md).

Workflow templating:
- Export your workflow from ComfyUI as API format JSON
  (Settings → Enable Dev mode → "Save (API Format)").
- Save it as workflows/ltx_2.3_v1.1.json.
- Run `python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json` to see
  every node and pick the IDs you need.
- Fill workflows/node_map.json (copy from node_map.example.json).

node_map.json — three accepted forms per logical name:
  "prompt_positive": "6"                                  ← shorthand
  "prompt_positive": {"node_id": "6", "input": "text"}    ← explicit input key
  "seed": [                                                ← list (all get same value)
    {"node_id": "4832", "input": "noise_seed"},
    {"node_id": "4967", "input": "noise_seed"}
  ]
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import CFG
from .logger import get

log = get("comfyui")

NODE_MAP_FILE = CFG.root / "workflows" / "node_map.json"
WORKFLOW_FILE = CFG.root / "workflows" / "ltx_2.3_v1.1.json"

# Default input key for each logical field when the shorthand string form is used.
_DEFAULT_INPUTS: dict[str, str] = {
    "prompt_positive": "text",
    "prompt_negative": "text",
    "input_image": "image",
    "seed": "seed",
    "output_filename": "filename_prefix",
}

# Logical fields that may be missing from node_map without warning.
_OPTIONAL_FIELDS = {"prompt_negative"}

# Timeouts. Upload: input PNG is small (<5MB). Download: video can be 10-50MB
# over a slow link, so we give it room.
_UPLOAD_TIMEOUT_S = 60.0
_DOWNLOAD_TIMEOUT_S = 600.0
_API_TIMEOUT_S = 30.0


@dataclass
class QueueStatus:
    prompt_id: str
    state: str  # "queued" | "running" | "done" | "failed" | "unknown"
    outputs: dict[str, Any]
    error: str | None = None


# ---------------------------------------------------------------------------
# node_map + workflow loading
# ---------------------------------------------------------------------------

def _normalize_item(logical: str, item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return {"node_id": item, "input": _DEFAULT_INPUTS.get(logical, "value")}
    if isinstance(item, dict) and "node_id" in item:
        return {
            "node_id": str(item["node_id"]),
            "input": item.get("input", _DEFAULT_INPUTS.get(logical, "value")),
        }
    raise ValueError(
        f"node_map[{logical!r}] item must be a string or "
        f"{{'node_id':..., 'input':...}}, got {item!r}"
    )


def _load_node_map() -> dict[str, list[dict[str, str]]]:
    if not NODE_MAP_FILE.exists():
        raise FileNotFoundError(
            f"{NODE_MAP_FILE} missing. Copy node_map.example.json to "
            "node_map.json and fill in your workflow's node IDs (use "
            "`python scripts/inspect_workflow.py` to find them)."
        )
    raw = json.loads(NODE_MAP_FILE.read_text(encoding="utf-8"))
    normalized: dict[str, list[dict[str, str]]] = {}
    for logical, value in raw.items():
        if logical.startswith("_"):
            continue
        items = value if isinstance(value, list) else [value]
        normalized[logical] = [_normalize_item(logical, it) for it in items]
    return normalized


def _load_workflow() -> dict[str, Any]:
    if not WORKFLOW_FILE.exists():
        raise FileNotFoundError(
            f"{WORKFLOW_FILE} missing. Export your workflow from ComfyUI "
            "in API format (Dev mode → Save API Format) and place it here."
        )
    return json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# upload / download (HTTP-only, works against remote ComfyUI)
# ---------------------------------------------------------------------------

def _upload_image(src: Path, *, dest_name: str | None = None, subfolder: str = "") -> str:
    """POST `src` to <host>/upload/image and return the value to put in a
    LoadImage node (`name` or `subfolder/name`).

    Uses overwrite=true so retries are idempotent under the same dest_name.
    """
    if not src.exists():
        raise FileNotFoundError(f"cannot upload missing image: {src}")

    name = dest_name or src.name
    if dest_name and src.suffix and not name.endswith(src.suffix):
        name = f"{name}{src.suffix}"

    with src.open("rb") as fh:
        files = {"image": (name, fh, "image/png")}
        data = {"type": "input", "subfolder": subfolder, "overwrite": "true"}
        resp = httpx.post(
            f"{CFG.comfyui_host}/upload/image",
            files=files, data=data, timeout=_UPLOAD_TIMEOUT_S,
        )
    resp.raise_for_status()
    info = resp.json()
    uploaded_name = info.get("name", name)
    uploaded_sub = info.get("subfolder", "") or ""
    log.info("uploaded image → %s/%s", uploaded_sub or "(root)", uploaded_name)
    return f"{uploaded_sub}/{uploaded_name}" if uploaded_sub else uploaded_name


def find_output_in_history(outputs: dict[str, Any], expected_prefix: str) -> dict | None:
    """Walk ComfyUI's per-node `outputs` and return the first item whose
    filename starts with `expected_prefix`.

    Returns a dict with keys `filename`, `subfolder`, `type` suitable for
    download_output(). Returns None if no match.
    """
    for _node_id, node_outs in outputs.items():
        for key in ("gifs", "videos", "images", "files"):
            for item in node_outs.get(key, []) or []:
                fname = item.get("filename") or ""
                if not fname.startswith(expected_prefix):
                    continue
                return {
                    "filename": fname,
                    "subfolder": item.get("subfolder", "") or "",
                    "type": item.get("type", "output"),
                }
    return None


def download_output(info: dict, target: Path) -> Path:
    """Stream-download a /view file into `target`. Returns the local path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    params = {
        "filename": info["filename"],
        "type": info.get("type", "output"),
        "subfolder": info.get("subfolder", ""),
    }
    with httpx.stream(
        "GET", f"{CFG.comfyui_host}/view",
        params=params, timeout=_DOWNLOAD_TIMEOUT_S,
    ) as resp:
        resp.raise_for_status()
        with target.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    log.info("downloaded → %s (%d bytes)", target, target.stat().st_size)
    return target


# ---------------------------------------------------------------------------
# prompt build + submit + status
# ---------------------------------------------------------------------------

def build_prompt(
    *,
    image_path: Path,
    positive_prompt: str,
    seed: int,
    output_filename_prefix: str,
    negative_prompt: str | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a ready-to-POST workflow dict with our values patched in.

    Patches (if mapped in node_map.json):
      - prompt_positive  → the per-clip positive prompt
      - prompt_negative  → the per-clip negative prompt (optional)
      - input_image      → the uploaded filename (after a POST to /upload/image)
      - seed             → patched on every node listed (list-form supported)
      - output_filename  → unique prefix so we can locate the MP4 later

    `overrides` lets you patch any other node:  overrides={"45": {"steps": 20}}
    """
    workflow = copy.deepcopy(_load_workflow())
    node_map = _load_node_map()

    def patch(logical: str, value: Any) -> None:
        items = node_map.get(logical)
        if not items:
            if logical not in _OPTIONAL_FIELDS:
                log.warning("node_map missing %r, skipping", logical)
            return
        for item in items:
            node_id, input_key = item["node_id"], item["input"]
            node = workflow.get(node_id)
            if not node:
                raise KeyError(
                    f"node id {node_id!r} (logical {logical!r}) not in workflow"
                )
            node.setdefault("inputs", {})[input_key] = value

    # Upload the cropped frame to ComfyUI. The prefix doubles as the unique
    # dest name, so retries overwrite the same input file on the server.
    uploaded = _upload_image(image_path, dest_name=f"{output_filename_prefix}_input")

    patch("prompt_positive", positive_prompt)
    if negative_prompt is not None:
        patch("prompt_negative", negative_prompt)
    patch("input_image", uploaded)
    patch("seed", seed)
    patch("output_filename", output_filename_prefix)

    if overrides:
        for node_id, kvs in overrides.items():
            workflow.setdefault(node_id, {}).setdefault("inputs", {}).update(kvs)
    return workflow


def submit(prompt_workflow: dict[str, Any]) -> str:
    """Submit a workflow to ComfyUI. Returns prompt_id."""
    body = {"prompt": prompt_workflow, "client_id": CFG.comfyui_client_id}
    resp = httpx.post(f"{CFG.comfyui_host}/prompt", json=body, timeout=_API_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {data!r}")
    log.info("queued prompt_id=%s (queue position %s)", prompt_id, data.get("number"))
    return prompt_id


def status(prompt_id: str) -> QueueStatus:
    """Resolve the live state of a prompt_id.

    State machine:
      - in /queue.queue_running → running
      - in /queue.queue_pending → queued
      - in /history with no error → done
      - in /history with error    → failed
      - else                       → unknown
    """
    queue = httpx.get(f"{CFG.comfyui_host}/queue", timeout=_API_TIMEOUT_S).json()
    for item in queue.get("queue_running", []):
        if _prompt_id_of(item) == prompt_id:
            return QueueStatus(prompt_id, "running", {})
    for item in queue.get("queue_pending", []):
        if _prompt_id_of(item) == prompt_id:
            return QueueStatus(prompt_id, "queued", {})

    hist = httpx.get(f"{CFG.comfyui_host}/history/{prompt_id}", timeout=_API_TIMEOUT_S).json()
    entry = hist.get(prompt_id)
    if entry:
        status_field = entry.get("status", {})
        if status_field.get("status_str") == "error":
            messages = status_field.get("messages", [])
            err = json.dumps(messages)[:2000]
            return QueueStatus(prompt_id, "failed", entry.get("outputs", {}), err)
        return QueueStatus(prompt_id, "done", entry.get("outputs", {}))

    return QueueStatus(prompt_id, "unknown", {})


def _prompt_id_of(queue_item: list[Any]) -> str | None:
    # ComfyUI queue items: [number, prompt_id, prompt, extra, outputs]
    return queue_item[1] if len(queue_item) > 1 else None


def wait_for(prompt_id: str, *, poll_interval_s: int | None = None,
             timeout_s: int | None = None) -> QueueStatus:
    """Block until terminal state. Use sparingly; prefer the poller daemon."""
    interval = poll_interval_s or CFG.poll_interval_s
    start = time.time()
    while True:
        st = status(prompt_id)
        if st.state in ("done", "failed"):
            return st
        if timeout_s and (time.time() - start) > timeout_s:
            return st
        time.sleep(interval)


def ping() -> bool:
    """Quick reachability check. Returns True if ComfyUI responds on /system_stats."""
    try:
        resp = httpx.get(f"{CFG.comfyui_host}/system_stats", timeout=5.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
