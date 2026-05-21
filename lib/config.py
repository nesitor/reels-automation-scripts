"""Central config loader. Resolves paths relative to the automation/ root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import json

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(env_key: str, default: str) -> Path:
    raw = os.getenv(env_key, default)
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


def _int(env_key: str, default: int) -> int:
    return int(os.getenv(env_key, str(default)))


def _bool(env_key: str, default: bool) -> bool:
    raw = os.getenv(env_key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _json_dict(env_key: str) -> dict[str, str]:
    raw = os.getenv(env_key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{env_key} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{env_key} must be a JSON object, got {type(value).__name__}")
    return {str(k): str(v) for k, v in value.items()}


@dataclass(frozen=True)
class Config:
    root: Path
    state_db_path: Path
    outputs_dir: Path
    guion_dir: Path
    google_api_key: str
    gemini_image_model: str
    google_ai_base_url: str | None
    google_ai_extra_headers: dict[str, str]

    comfyui_host: str
    comfyui_client_id: str

    telegram_bot_token: str
    telegram_chat_id: str
    telegram_enabled: bool

    watermark_crop_px: int
    image_max_attempts: int
    video_max_attempts: int
    poll_interval_s: int


def load() -> Config:
    return Config(
        root=ROOT,
        state_db_path=_path("STATE_DB_PATH", "db/state.db"),
        outputs_dir=_path("OUTPUTS_DIR", "outputs"),
        guion_dir=_path("GUION_DIR", "guion"),
        google_api_key=os.getenv("GOOGLE_AI_STUDIO_API_KEY", ""),
        google_ai_base_url=(os.getenv("GOOGLE_AI_BASE_URL") or None),
        google_ai_extra_headers=_json_dict("GOOGLE_AI_EXTRA_HEADERS"),
        gemini_image_model=os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image-preview"),
        comfyui_host=os.getenv("COMFYUI_HOST", "http://127.0.0.1:8188").rstrip("/"),
        comfyui_client_id=os.getenv("COMFYUI_CLIENT_ID", "aspectados-reels-automation"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_enabled=_bool("TELEGRAM_ENABLED", True),
        watermark_crop_px=_int("WATERMARK_CROP_PX", 100),
        image_max_attempts=_int("IMAGE_MAX_ATTEMPTS", 3),
        video_max_attempts=_int("VIDEO_MAX_ATTEMPTS", 2),
        poll_interval_s=_int("POLL_INTERVAL_S", 60),
    )


CFG = load()
