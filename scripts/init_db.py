"""Initialize / migrate the SQLite state DB. Idempotent — safe to re-run."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db
from lib.config import CFG
from lib.logger import get

log = get("init_db")


def main() -> int:
    log.info("initializing schema at %s", CFG.state_db_path)
    db.init_schema()
    log.info("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
