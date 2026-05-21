"""Rich-formatted logger that also emits structured events to the DB."""
from __future__ import annotations

import json
import logging
from typing import Any

from rich.logging import RichHandler

from . import db

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True, show_path=False)],
)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def event(
    stage: str,
    message: str,
    *,
    run_id: int | None = None,
    clip_id: int | None = None,
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    """Persist a structured event in `events` and mirror to the stdout logger."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO events(run_id, clip_id, stage, level, message, payload) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, clip_id, stage, level, message, json.dumps(payload) if payload else None),
        )
    logger = get(stage)
    log_fn = getattr(logger, level if level != "warn" else "warning", logger.info)
    log_fn(message)
