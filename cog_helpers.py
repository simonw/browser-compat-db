"""Helpers used by the cog blocks in README.md.

Run ``uv run cog -r README.md`` to regenerate the README sections from a built
``browser-compat.db`` (build it first with ``uv run build_db.py``).
"""

from __future__ import annotations

import sqlite_utils

DB_PATH = "browser-compat.db"

# Order tables logically rather than alphabetically for the README.
TABLE_ORDER = [
    "browsers",
    "browser_releases",
    "features",
    "feature_tags",
    "feature_spec_urls",
    "support",
    "support_flags",
    "metadata",
]


def _db() -> sqlite_utils.Database:
    return sqlite_utils.Database(DB_PATH)


def metadata_block() -> str:
    db = _db()
    meta = {r["key"]: r["value"] for r in db["metadata"].rows}
    lines = [
        f"- **Data last updated:** {meta.get('data_last_updated', 'unknown')}",
        f"- **Source commit:** `{meta.get('data_commit', 'unknown')}`",
        f"- **browser-compat-data version:** {meta.get('bcd_version', 'unknown')}",
        f"- **Imported at:** {meta.get('imported_at', 'unknown')}",
    ]
    return "\n".join(lines)


def table_counts() -> str:
    db = _db()
    names = [t for t in TABLE_ORDER if t in db.table_names()]
    names += [t for t in db.table_names() if t not in names]
    rows = ["| Table | Rows |", "| --- | --- |"]
    for name in names:
        rows.append(f"| `{name}` | {db[name].count:,} |")
    return "\n".join(rows)


def schema() -> str:
    db = _db()
    return db.schema
