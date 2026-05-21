"""Nano Banana 2 / Gemini Image API wrapper (Google AI Studio free tier).

The free tier rate limit is generous for our use (16 calls + a handful of
protagonist variations). We retry transient errors with exponential backoff.
"""
from __future__ import annotations

import io
import random
import time
from pathlib import Path
from typing import Sequence

from google import genai
from google.genai import types
from PIL import Image

from .config import CFG
from .logger import get

log = get("nano_banana")

_client: genai.Client | None = None


def _client_singleton() -> genai.Client:
    global _client
    if _client is None:
        if not CFG.google_api_key:
            raise RuntimeError(
                "GOOGLE_AI_STUDIO_API_KEY not set. Get a free key at "
                "https://aistudio.google.com/apikey — or point at a proxy "
                "via GOOGLE_AI_BASE_URL and put whatever key the proxy "
                "expects in this variable."
            )
        # Build HttpOptions only when the user actually overrides defaults.
        # Some SDK versions reject HttpOptions with everything-empty fields,
        # so we keep it None unless we have something to set.
        http_options = None
        if CFG.google_ai_base_url or CFG.google_ai_extra_headers:
            http_options = types.HttpOptions(
                base_url=CFG.google_ai_base_url,
                headers=CFG.google_ai_extra_headers or None,
            )
            log.info(
                "using custom Gemini endpoint base_url=%s (extra headers: %d)",
                CFG.google_ai_base_url or "<default>",
                len(CFG.google_ai_extra_headers or {}),
            )
        _client = genai.Client(
            api_key=CFG.google_api_key,
            http_options=http_options,
        )
    return _client


def _backoff_attempts(max_attempts: int = 4):
    """Yield (attempt, sleep_before) tuples with exponential backoff + jitter."""
    for attempt in range(1, max_attempts + 1):
        sleep = 0.0 if attempt == 1 else min(60.0, (2 ** attempt) + random.random())
        yield attempt, sleep


def generate_image(
    prompt: str,
    *,
    out_path: Path,
    reference_images: Sequence[Path] = (),
    seed: int | None = None,
    max_attempts: int = 4,
) -> Path:
    """Generate one image. Saves PNG to `out_path` and returns it.

    `reference_images` are passed as multimodal context, e.g. the protagonist
    base image, so the model preserves facial identity across clips.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    client = _client_singleton()

    parts: list = []
    for ref in reference_images:
        with Image.open(ref) as im:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            parts.append(
                types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")
            )
    parts.append(types.Part.from_text(text=prompt))

    last_err: Exception | None = None
    for attempt, sleep in _backoff_attempts(max_attempts):
        if sleep:
            log.info("retry attempt=%d after %.1fs", attempt, sleep)
            time.sleep(sleep)
        try:
            response = client.models.generate_content(
                model=CFG.gemini_image_model,
                contents=parts,
                config=types.GenerateContentConfig(
                    seed=seed,
                    response_modalities=["IMAGE"],
                ),
            )
            for cand in response.candidates or []:
                for part in cand.content.parts or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        with Image.open(io.BytesIO(inline.data)) as im:
                            im.save(out_path, format="PNG", optimize=True)
                        log.info("image saved → %s", out_path.name)
                        return out_path
            raise RuntimeError("response did not contain inline image data")
        except Exception as exc:  # noqa: BLE001 — we want to retry anything transient
            last_err = exc
            log.warning("generate attempt %d failed: %s", attempt, exc)

    raise RuntimeError(f"image generation failed after {max_attempts} attempts: {last_err}")
