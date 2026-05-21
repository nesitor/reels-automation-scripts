"""Image helpers: crop the Nano Banana 2 watermark and keep 9:16 aspect."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from .config import CFG


def crop_watermark(src: Path, dst: Path, *, crop_px: int | None = None) -> Path:
    """Crop `crop_px` pixels from the bottom of `src` and rescale back to the
    original dimensions, preserving aspect ratio. Returns `dst`.

    Why rescale: LTX expects exact 9:16 frames. Cropping alone would change the
    aspect ratio; scaling back keeps the model happy and only loses a tiny
    sliver of vertical content (the watermark itself).
    """
    crop = crop_px if crop_px is not None else CFG.watermark_crop_px
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        w, h = im.size
        if crop >= h:
            raise ValueError(f"crop_px={crop} >= image height {h}")
        cropped = im.crop((0, 0, w, h - crop))
        rescaled = cropped.resize((w, h), Image.LANCZOS)
        rescaled.save(dst, format="PNG", optimize=True)
    return dst
