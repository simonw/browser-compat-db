"""Load mdn/browser-compat-data into a normalized SQLite database.

Usage:
    uv run build_db.py /path/to/browser-compat-data [--db browser-compat.db]

The script is idempotent: running it again against a refreshed checkout of the
data repository updates the database in place, including handling features,
releases and browsers that were added, changed or removed upstream.

Every record is validated through the Pydantic models in ``models.py`` before
it reaches the database, so an upstream change to the JSON shape surfaces as a
loud ``ValidationError`` rather than silently corrupting the import.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sqlite_utils
from pydantic import ValidationError

from models import (
    BrowsersFile,
    CompatStatement,
    SimpleSupportStatement,
)

# Top-level directories in the data repo that contain compat data trees.
COMPAT_CATEGORIES = [
    "api",
    "css",
    "html",
    "http",
    "javascript",
    "mathml",
    "mediatypes",
    "svg",
    "webassembly",
    "webdriver",
    "webextensions",
]


# ---------------------------------------------------------------------------
# Helpers for turning raw values into database-friendly scalars
# ---------------------------------------------------------------------------


def join_text(value: Any) -> Optional[str]:
    """Collapse a string-or-list-of-strings field into a single string."""
    if value is None:
        return None
    if isinstance(value, list):
        return "\n".join(value)
    return value


def version_scalar(value: Any) -> Tuple[Optional[str], Optional[bool]]:
    """Normalize a version_added/version_removed value.

    Returns ``(version_string, known_flag)`` where:
      * a version string ("57", "≤10", "preview") -> (string, True)
      * ``True``  -> (None, True)   supported, version unknown
      * ``False`` -> (None, False)  explicitly not supported
      * ``None``  -> (None, None)   unknown
    """
    if value is None:
        return None, None
    if value is True:
        return None, True
    if value is False:
        return None, False
    return str(value), True


# ---------------------------------------------------------------------------
# Parsing browser metadata
# ---------------------------------------------------------------------------


def parse_browsers(data_dir: Path) -> Tuple[List[dict], List[dict]]:
    """Return (browser_rows, release_rows) from browsers/*.json."""
    browser_rows: List[dict] = []
    release_rows: List[dict] = []
    for path in sorted((data_dir / "browsers").glob("*.json")):
        raw = json.loads(path.read_text())
        try:
            parsed = BrowsersFile.model_validate(raw)
        except ValidationError as exc:
            raise SystemExit(
                f"Browser schema drift detected in {path}:\n{exc}"
            ) from exc
        for browser_id, browser in parsed.browsers.items():
            browser_rows.append(
                {
                    "id": browser_id,
                    "name": browser.name,
                    "type": browser.type,
                    "upstream": browser.upstream,
                    "preview_name": browser.preview_name,
                    "pref_url": browser.pref_url,
                    "accepts_flags": browser.accepts_flags,
                    "accepts_webextensions": browser.accepts_webextensions,
                }
            )
            for version, release in browser.releases.items():
                release_rows.append(
                    {
                        "browser_id": browser_id,
                        "version": version,
                        "release_date": release.release_date,
                        "release_notes": release.release_notes,
                        "status": release.status,
                        "engine": release.engine,
                        "engine_version": release.engine_version,
                    }
                )
    return browser_rows, release_rows


# ---------------------------------------------------------------------------
# Parsing compat data trees
# ---------------------------------------------------------------------------


class CompatParser:
    """Recursively walks the nested compat JSON producing flat rows."""

    def __init__(self) -> None:
        self.features: List[dict] = []
        self.tags: List[dict] = []
        self.spec_urls: List[dict] = []
        self.support: List[dict] = []
        self.flags: List[dict] = []

    def parse_file(self, path: Path, category: str, source_file: str) -> None:
        raw = json.loads(path.read_text())
        # Each file's top level is the category key(s); descend into them.
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            self._walk(value, [key], category, source_file, parent_id=None)

    def _walk(
        self,
        node: dict,
        path: List[str],
        category: str,
        source_file: str,
        parent_id: Optional[str],
    ) -> None:
        feature_id = ".".join(path)
        new_parent = parent_id
        if "__compat" in node:
            self._add_feature(
                feature_id, path, category, source_file, parent_id, node["__compat"]
            )
            new_parent = feature_id
        for key, value in node.items():
            if key == "__compat":
                continue
            if isinstance(value, dict):
                self._walk(
                    value, path + [key], category, source_file, new_parent
                )

    def _add_feature(
        self,
        feature_id: str,
        path: List[str],
        category: str,
        source_file: str,
        parent_id: Optional[str],
        raw_compat: Any,
    ) -> None:
        try:
            compat = CompatStatement.model_validate(raw_compat)
        except ValidationError as exc:
            raise SystemExit(
                f"Compat schema drift detected at {feature_id} "
                f"({source_file}):\n{exc}"
            ) from exc

        status = compat.status
        self.features.append(
            {
                "id": feature_id,
                "category": category,
                "name": path[-1],
                "parent_id": parent_id,
                "depth": len(path),
                "description": compat.description,
                "mdn_url": compat.mdn_url,
                "source_file": source_file,
                "experimental": status.experimental if status else None,
                "standard_track": status.standard_track if status else None,
                "deprecated": status.deprecated if status else None,
            }
        )

        for tag in compat.tags or []:
            self.tags.append({"feature_id": feature_id, "tag": tag})

        spec = compat.spec_url
        spec_urls = spec if isinstance(spec, list) else ([spec] if spec else [])
        for spec_url in spec_urls:
            self.spec_urls.append(
                {"feature_id": feature_id, "spec_url": spec_url}
            )

        for browser_id, entry in compat.support.items():
            statements = entry if isinstance(entry, list) else [entry]
            for index, statement in enumerate(statements):
                self._add_support(feature_id, browser_id, index, statement)

    def _add_support(
        self,
        feature_id: str,
        browser_id: str,
        index: int,
        statement: Any,
    ) -> None:
        if statement == "mirror":
            self.support.append(
                {
                    "feature_id": feature_id,
                    "browser_id": browser_id,
                    "statement_index": index,
                    "is_mirror": True,
                    "version_added": None,
                    "version_removed": None,
                    "supported": None,
                    "prefix": None,
                    "alternative_name": None,
                    "partial_implementation": None,
                    "impl_url": None,
                    "notes": None,
                }
            )
            return

        assert isinstance(statement, SimpleSupportStatement)
        added, added_known = version_scalar(statement.version_added)
        removed, _ = version_scalar(statement.version_removed)
        # supported = was it ever added (and not a plain False)?
        self.support.append(
            {
                "feature_id": feature_id,
                "browser_id": browser_id,
                "statement_index": index,
                "is_mirror": False,
                "version_added": added,
                "version_removed": removed,
                "supported": added_known,
                "prefix": statement.prefix,
                "alternative_name": statement.alternative_name,
                "partial_implementation": statement.partial_implementation,
                "impl_url": join_text(statement.impl_url),
                "notes": join_text(statement.notes),
            }
        )

        for flag_index, flag in enumerate(statement.flags or []):
            self.flags.append(
                {
                    "feature_id": feature_id,
                    "browser_id": browser_id,
                    "statement_index": index,
                    "flag_index": flag_index,
                    "type": flag.type,
                    "name": flag.name,
                    "value_to_set": flag.value_to_set,
                }
            )


def parse_compat(data_dir: Path) -> CompatParser:
    parser = CompatParser()
    for category in COMPAT_CATEGORIES:
        category_dir = data_dir / category
        if not category_dir.is_dir():
            continue
        for path in sorted(category_dir.rglob("*.json")):
            source_file = str(path.relative_to(data_dir))
            parser.parse_file(path, category, source_file)
    return parser


# ---------------------------------------------------------------------------
# Metadata about the source checkout
# ---------------------------------------------------------------------------


def source_metadata(data_dir: Path) -> Dict[str, Optional[str]]:
    """Collect version + git information about the data checkout."""
    meta: Dict[str, Optional[str]] = {}

    package_json = data_dir / "package.json"
    if package_json.exists():
        meta["bcd_version"] = json.loads(package_json.read_text()).get("version")

    def git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(
                ["git", "-C", str(data_dir), *args],
                capture_output=True,
                text=True,
                check=True,
            )
            return out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    meta["data_commit"] = git("rev-parse", "HEAD")
    meta["data_last_updated"] = git("log", "-1", "--format=%cI")
    meta["imported_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return meta


# ---------------------------------------------------------------------------
# Writing to SQLite
# ---------------------------------------------------------------------------


def sync_table(
    db: sqlite_utils.Database,
    name: str,
    records: List[dict],
    pk,
    foreign_keys=None,
) -> None:
    """Upsert all records, then delete rows whose PK is no longer present.

    This keeps the table in sync with the source on every run, handling
    additions, updates and deletions without recreating the database.
    """
    table = db[name]
    if not records:
        # Nothing in the source: clear the table if it exists.
        if table.exists():
            table.delete_where()
        return

    table.upsert_all(records, pk=pk, foreign_keys=foreign_keys or [], alter=True)

    # Prune rows that disappeared upstream.
    pk_cols = [pk] if isinstance(pk, str) else list(pk)
    current_keys = {
        tuple(r[c] for c in pk_cols) for r in records
    }
    placeholders = ", ".join("?" for _ in pk_cols)
    existing = list(
        table.rows_where(select=", ".join(f'"{c}"' for c in pk_cols))
    )
    to_delete = [
        tuple(row[c] for c in pk_cols)
        for row in existing
        if tuple(row[c] for c in pk_cols) not in current_keys
    ]
    if to_delete:
        where = " AND ".join(f'"{c}" = ?' for c in pk_cols)
        with db.conn:
            for key in to_delete:
                db.conn.execute(
                    f'DELETE FROM "{name}" WHERE {where}', key
                )


def build(data_dir: Path, db_path: Path) -> Dict[str, Any]:
    browser_rows, release_rows = parse_browsers(data_dir)
    compat = parse_compat(data_dir)
    meta = source_metadata(data_dir)

    db = sqlite_utils.Database(db_path)

    sync_table(db, "browsers", browser_rows, pk="id")
    sync_table(
        db,
        "browser_releases",
        release_rows,
        pk=("browser_id", "version"),
        foreign_keys=[("browser_id", "browsers", "id")],
    )
    sync_table(
        db,
        "features",
        compat.features,
        pk="id",
        foreign_keys=[("parent_id", "features", "id")],
    )
    sync_table(
        db,
        "feature_tags",
        compat.tags,
        pk=("feature_id", "tag"),
        foreign_keys=[("feature_id", "features", "id")],
    )
    sync_table(
        db,
        "feature_spec_urls",
        compat.spec_urls,
        pk=("feature_id", "spec_url"),
        foreign_keys=[("feature_id", "features", "id")],
    )
    sync_table(
        db,
        "support",
        compat.support,
        pk=("feature_id", "browser_id", "statement_index"),
        foreign_keys=[
            ("feature_id", "features", "id"),
            ("browser_id", "browsers", "id"),
        ],
    )
    sync_table(
        db,
        "support_flags",
        compat.flags,
        pk=("feature_id", "browser_id", "statement_index", "flag_index"),
        foreign_keys=[("feature_id", "features", "id")],
    )

    # Key/value metadata table.
    db["metadata"].upsert_all(
        [{"key": k, "value": v} for k, v in meta.items()],
        pk="key",
        alter=True,
    )

    create_indexes(db)
    db.index_foreign_keys()
    db.vacuum()
    return meta


def create_indexes(db: sqlite_utils.Database) -> None:
    index_specs = {
        "features": [["category"], ["deprecated"], ["experimental"]],
        "browser_releases": [["release_date"], ["status"]],
        "support": [["browser_id"], ["version_added"], ["supported"]],
    }
    for table, specs in index_specs.items():
        for columns in specs:
            db[table].create_index(columns, if_not_exists=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data_dir",
        type=Path,
        help="Path to a checkout of mdn/browser-compat-data",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("browser-compat.db"),
        help="Path to the SQLite database to create/update",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.data_dir.is_dir():
        raise SystemExit(f"Data directory not found: {args.data_dir}")

    meta = build(args.data_dir, args.db)
    db = sqlite_utils.Database(args.db)
    print(f"Wrote {args.db}")
    print(f"  bcd version:      {meta.get('bcd_version')}")
    print(f"  data last update: {meta.get('data_last_updated')}")
    for table in db.table_names():
        print(f"  {table}: {db[table].count:,} rows")


if __name__ == "__main__":
    main()
