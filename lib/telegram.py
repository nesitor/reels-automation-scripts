"""Telegram bot notifier with deduplication via the `notifications` table.

Same event_key is only sent once across the lifetime of the DB. Use this to
ping the user when a stage completes or a clip finishes/fails.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from . import db
from .config import CFG
from .logger import get

log = get("telegram")


def _key(parts: tuple[str, ...]) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24]


def send(
    event_key_parts: tuple[str, ...],
    text: str,
    *,
    parse_mode: str = "Markdown",
    payload: dict[str, Any] | None = None,
) -> bool:
    """Send a Telegram message. Idempotent on (event_key_parts).

    Returns True if the message was sent now, False if skipped (dedupe, disabled,
    or unconfigured).
    """
    event_key = _key(event_key_parts)

    with db.connect() as conn:
        existing = conn.execute(
            "SELECT delivery_status FROM notifications WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing and existing["delivery_status"] == "sent":
            return False
        if not existing:
            conn.execute(
                "INSERT INTO notifications(event_key, channel, payload) VALUES (?,?,?)",
                (event_key, "telegram", json.dumps(payload) if payload else None),
            )

    if not CFG.telegram_enabled:
        log.info("telegram disabled, would have sent: %s", text[:80])
        return False
    if not CFG.telegram_bot_token or not CFG.telegram_chat_id:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing; skipping send")
        return False

    url = f"https://api.telegram.org/bot{CFG.telegram_bot_token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            data={
                "chat_id": CFG.telegram_chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": "true",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("telegram send failed: %s", exc)
        with db.connect() as conn:
            conn.execute(
                "UPDATE notifications SET delivery_status='failed', error=? WHERE event_key=?",
                (str(exc), event_key),
            )
        return False

    with db.connect() as conn:
        conn.execute(
            "UPDATE notifications SET delivery_status='sent' WHERE event_key=?",
            (event_key,),
        )
    log.info("telegram → %s", text[:80])
    return True
