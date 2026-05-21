"""Smoke-test an OpenAI-compatible image generation proxy.

Reads credentials from automation/.env (the same GOOGLE_AI_* vars the
pipeline uses), with optional PROXY_* overrides. By default tries every
known endpoint shape until one returns an image; use --method to isolate
a single attempt and see exactly where it hangs / fails.

Usage:
    # Default — try every method until one works
    python scripts/test_proxy_image.py
    python scripts/test_proxy_image.py "una manzana roja sobre una mesa"

    # Only one method (useful for isolating which one hangs)
    python scripts/test_proxy_image.py --method chat-stream
    python scripts/test_proxy_image.py --method chat-nostream
    python scripts/test_proxy_image.py --method images

    # Shorter timeout while debugging
    python scripts/test_proxy_image.py --method chat-stream --timeout 30

    # Override creds without touching .env
    PROXY_BASE_URL=https://my-proxy/v1 PROXY_API_KEY=sk-xxx \\
        python scripts/test_proxy_image.py --method chat-stream

Methods:
    chat-stream   POST /chat/completions with stream=true  (SSE parsing)
    chat-nostream POST /chat/completions with stream=false (single JSON body)
    images        POST /images/generations (DALL-E shape)
    all           run chat-stream → chat-nostream → images in that order
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import httpx

DEFAULT_PROMPT = (
    "A photorealistic close-up of a single ripe red apple on a warm wooden "
    "kitchen table, soft natural window light from the side, shallow depth "
    "of field, 35mm film grain, no text, no logos."
)

_parser = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
)
_parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT,
                     help="Prompt to send (default: a test apple prompt).")
_parser.add_argument("--method", choices=["chat-stream", "chat-nostream", "images", "all"],
                     default="all",
                     help="Which endpoint to test. Default: all (chat-stream → "
                          "chat-nostream → images).")
_parser.add_argument("--timeout", type=float, default=300.0,
                     help="Read timeout in seconds (default: 300). Lower it "
                          "while debugging hangs.")
_parser.add_argument("--connect-timeout", type=float, default=10.0,
                     help="Connect timeout in seconds (default: 10).")
_parser.add_argument("--out", type=Path, default=Path("test_proxy.png"))
ARGS = _parser.parse_args()

BASE = (os.getenv("PROXY_BASE_URL") or os.getenv("GOOGLE_AI_BASE_URL", "")).rstrip("/")
KEY = os.getenv("PROXY_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_API_KEY", "")
MODEL = os.getenv("PROXY_IMAGE_MODEL") or os.getenv("GEMINI_IMAGE_MODEL", "")
PROMPT = ARGS.prompt
OUT = ARGS.out
TIMEOUT = httpx.Timeout(ARGS.timeout, connect=ARGS.connect_timeout)

if not BASE:
    sys.exit("✗ set GOOGLE_AI_BASE_URL (or PROXY_BASE_URL)")
if not KEY:
    sys.exit("✗ set GOOGLE_AI_STUDIO_API_KEY (or PROXY_API_KEY)")
if not MODEL:
    sys.exit("✗ set GEMINI_IMAGE_MODEL (or PROXY_IMAGE_MODEL)")

HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}
try:
    for k, v in (json.loads(os.getenv("GOOGLE_AI_EXTRA_HEADERS", "{}") or "{}")).items():
        HEADERS[str(k)] = str(v)
except json.JSONDecodeError:
    pass

print(f"BASE    = {BASE}")
print(f"MODEL   = {MODEL}")
print(f"METHOD  = {ARGS.method}")
print(f"TIMEOUT = connect={ARGS.connect_timeout}s  read={ARGS.timeout}s")
print(f"PROMPT  = {PROMPT[:90]}{'…' if len(PROMPT) > 90 else ''}")
print()


def _fetch_url(url: str) -> bytes:
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    r = httpx.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def _extract_image(data: dict) -> bytes | None:
    """Try every known OpenAI-compatible response shape that carries an image."""
    msg = ((data.get("choices") or [{}])[0]).get("message", {}) or {}

    # message.images[].image_url.url (gpt-4o style image output)
    for img in msg.get("images") or []:
        url = (img.get("image_url") or {}).get("url") or img.get("url")
        if url:
            return _fetch_url(url)

    content = msg.get("content")
    # content as a list of multimodal parts
    if isinstance(content, list):
        for part in content:
            ptype = part.get("type", "")
            if "image" in ptype:
                url = (part.get("image_url") or {}).get("url") or part.get("url")
                if url:
                    return _fetch_url(url)
                if part.get("data"):
                    return base64.b64decode(part["data"])
    # content as a string with embedded data URI or markdown image
    if isinstance(content, str):
        m = re.search(r"data:image/[\w+\-.]+;base64,([A-Za-z0-9+/=]+)", content)
        if m:
            return base64.b64decode(m.group(1))
        m = re.search(r"!\[[^\]]*\]\((https?://[^\)\s]+)\)", content)
        if m:
            return _fetch_url(m.group(1))

    # Images-API shape
    for item in data.get("data") or []:
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return _fetch_url(item["url"])

    return None


def _parse_response_body(r: httpx.Response) -> dict | None:
    """Return a dict from the response, handling JSON, SSE streaming, raw PNG."""
    ct = r.headers.get("content-type", "").lower()
    print(f"  content-type={ct or '(none)'}  bytes={len(r.content)}")

    # Raw image: some proxies return the PNG body directly when the model
    # produces a single image. Surface it as a synthetic JSON shape.
    if ct.startswith("image/"):
        return {"data": [{"b64_json": base64.b64encode(r.content).decode("ascii")}]}

    # JSON
    try:
        return r.json()
    except json.JSONDecodeError:
        pass

    # SSE: lines of "data: {...}\n\n", final "data: [DONE]"
    text = r.text or ""
    if "data:" in text:
        merged: dict = {"choices": [{"message": {"content": "", "images": []}}]}
        merged_content = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # Standard chat-completions streaming chunk
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or ch.get("message") or {}
                if isinstance(delta.get("content"), str):
                    merged_content.append(delta["content"])
                elif isinstance(delta.get("content"), list):
                    for part in delta["content"]:
                        if "image" in part.get("type", ""):
                            merged["choices"][0]["message"].setdefault("content", [])
                            if isinstance(merged["choices"][0]["message"]["content"], list):
                                merged["choices"][0]["message"]["content"].append(part)
                            else:
                                merged["choices"][0]["message"]["content"] = [part]
                for img in delta.get("images") or []:
                    merged["choices"][0]["message"]["images"].append(img)
        if merged_content and not isinstance(merged["choices"][0]["message"].get("content"), list):
            merged["choices"][0]["message"]["content"] = "".join(merged_content)
        print(f"  parsed SSE: {len(merged_content)} content chunks, "
              f"{len(merged['choices'][0]['message']['images'])} image refs")
        return merged

    # Nothing recognised — dump first 500 chars so we can see what came back
    preview = text[:500] if text else f"<{len(r.content)} bytes of binary, first 40: {r.content[:40]!r}>"
    print(f"  unrecognised body. preview:\n  {preview}\n")
    return None


def _try_chat_completions(*, stream: bool) -> bytes | None:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": stream,
        "modalities": ["image", "text"],
    }
    url = f"{BASE}/chat/completions"
    print(f"→ POST {url}  stream={stream}")
    try:
        # Stream the response so we don't sit on a blocked socket waiting for
        # the whole thing to arrive. We still collect the full body before
        # parsing, but with stream=True httpx reads chunks as they come.
        with httpx.stream("POST", url, headers=HEADERS, json=body,
                          timeout=TIMEOUT) as r:
            print(f"  status={r.status_code}  content-type={r.headers.get('content-type','')}")
            if r.status_code != 200:
                body_text = r.read().decode("utf-8", errors="replace")
                print(f"  body={body_text[:800]}\n")
                return None
            # Read the raw body fully — _parse_response_body handles JSON/SSE/PNG
            raw = r.read()
            # Build a synthetic Response so _parse_response_body can use .json()/.text/.content
            fake = httpx.Response(
                200, headers=r.headers, content=raw, request=r.request,
            )
            data = _parse_response_body(fake)
    except httpx.HTTPError as exc:
        print(f"  transport error: {exc}\n")
        return None
    if data is None:
        return None
    img = _extract_image(data)
    if img:
        return img
    print("  no image in chat-completions response. Response shape:")
    print(f"  {json.dumps(data, indent=2)[:1500]}\n")
    return None


def try_chat_stream() -> bytes | None:
    return _try_chat_completions(stream=True)


def try_chat_nostream() -> bytes | None:
    return _try_chat_completions(stream=False)


def try_images_api() -> bytes | None:
    body = {
        "model": MODEL,
        "prompt": PROMPT,
        "n": 1,
        "response_format": "b64_json",
    }
    url = f"{BASE}/images/generations"
    print(f"→ POST {url}")
    try:
        r = httpx.post(url, headers=HEADERS, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        print(f"  transport error: {exc}\n")
        return None
    print(f"  status={r.status_code}")
    if r.status_code != 200:
        print(f"  body={r.text[:800]}\n")
        return None
    data = _parse_response_body(r)
    if data is None:
        return None
    img = _extract_image(data)
    if img:
        return img
    print("  no image in images-api response. Response shape:")
    print(f"  {json.dumps(data, indent=2)[:1500]}\n")
    return None


_METHODS = {
    "chat-stream": try_chat_stream,
    "chat-nostream": try_chat_nostream,
    "images": try_images_api,
}
if ARGS.method == "all":
    to_run = [("chat-stream", try_chat_stream),
              ("chat-nostream", try_chat_nostream),
              ("images", try_images_api)]
else:
    to_run = [(ARGS.method, _METHODS[ARGS.method])]

for name, attempt in to_run:
    print(f"=== method: {name} ===")
    img_bytes = attempt()
    if img_bytes:
        OUT.write_bytes(img_bytes)
        print(f"\n✓ {name} returned an image — saved {OUT.resolve()} ({len(img_bytes):,} bytes)")
        sys.exit(0)
    print()

sys.exit(f"\n✗ method(s) {[n for n,_ in to_run]} returned no image — see responses above for clues")
