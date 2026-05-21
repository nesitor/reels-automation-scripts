"""Project-relative path handling.

The state DB stores every file path RELATIVE to the project root, so the
whole folder can be moved (or cloned elsewhere) without breaking. The
contract is symmetric:

  - producers call `rel()` right before writing a path into the DB;
  - consumers call `resolve()` right after reading one back.

`resolve()` also accepts absolute paths unchanged, so rows written by older
versions of the pipeline (absolute) keep working until migrated.
"""
from __future__ import annotations

from pathlib import Path

from .config import CFG

ROOT = CFG.root

# Where adopt_assets.py copies assets that live OUTSIDE the project tree,
# so they too end up reachable through a stable project-relative path.
# Layout: imported_assets/<run_id>/<category>/clip_NN<suffix><ext>
IMPORTED_ASSETS = ROOT / "imported_assets"


def _abs(path: str | Path) -> Path:
    """Resolve `path` to an absolute, symlink-free Path.

    A relative path is interpreted as relative to the project ROOT (the DB
    convention) — not the current working directory.
    """
    p = Path(path)
    return (p if p.is_absolute() else ROOT / p).resolve()


def rel(path: str | Path) -> str:
    """Return `path` as a string relative to the project root.

    Call this before storing any file path in the DB. Paths that fall
    outside the project tree are returned absolute (resolve() round-trips
    both forms) — but adopt_assets.py copies external assets inside the
    tree precisely so that fallback rarely triggers.
    """
    abs_p = _abs(path)
    try:
        return str(abs_p.relative_to(ROOT))
    except ValueError:
        return str(abs_p)


def resolve(stored: str | Path) -> Path:
    """Turn a DB-stored path back into a usable absolute Path.

    Relative paths are resolved against the project root; absolute paths
    (legacy rows, or out-of-tree assets) are returned unchanged.
    """
    p = Path(stored)
    return p if p.is_absolute() else (ROOT / p)


def is_inside(path: str | Path) -> bool:
    """True if `path` lives inside the project tree.

    A relative `path` is resolved against the current working directory
    here (callers pass already-absolute paths), matching how the OS would
    open it.
    """
    abs_p = Path(path).resolve()
    return abs_p == ROOT or ROOT in abs_p.parents
