"""Tests for build_db.py using a small synthetic data directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite_utils

import build_db
from models import CompatStatement, BrowsersFile
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Fixtures: a miniature browser-compat-data checkout
# ---------------------------------------------------------------------------


def write_data_dir(tmp_path: Path, *, with_grid: bool = True) -> Path:
    data = tmp_path / "data"
    (data / "browsers").mkdir(parents=True)
    (data / "css" / "properties").mkdir(parents=True)

    (data / "package.json").write_text(json.dumps({"version": "9.9.9"}))

    (data / "browsers" / "chrome.json").write_text(
        json.dumps(
            {
                "browsers": {
                    "chrome": {
                        "name": "Chrome",
                        "type": "desktop",
                        "accepts_flags": True,
                        "accepts_webextensions": True,
                        "releases": {
                            "1": {
                                "release_date": "2008-12-11",
                                "status": "retired",
                                "engine": "WebKit",
                            },
                            "57": {
                                "release_date": "2017-03-09",
                                "status": "current",
                                "engine": "Blink",
                            },
                        },
                    },
                    "chrome_android": {
                        "name": "Chrome Android",
                        "type": "mobile",
                        "upstream": "chrome",
                        "releases": {},
                    },
                }
            }
        )
    )

    flexbox = {
        "css": {
            "properties": {
                "flex": {
                    "__compat": {
                        "mdn_url": "https://example/flex",
                        "spec_url": ["https://spec/a", "https://spec/b"],
                        "tags": ["web-features:flexbox"],
                        "support": {
                            "chrome": {"version_added": "29"},
                            "chrome_android": "mirror",
                        },
                        "status": {
                            "experimental": False,
                            "standard_track": True,
                            "deprecated": False,
                        },
                    }
                }
            }
        }
    }
    (data / "css" / "properties" / "flex.json").write_text(json.dumps(flexbox))

    if with_grid:
        grid = {
            "css": {
                "properties": {
                    "grid": {
                        "__compat": {
                            "support": {
                                "chrome": [
                                    {"version_added": "57"},
                                    {
                                        "version_added": True,
                                        "version_removed": "10",
                                        "flags": [
                                            {
                                                "type": "preference",
                                                "name": "enable-grid",
                                                "value_to_set": "true",
                                            }
                                        ],
                                    },
                                ],
                                "chrome_android": {"version_added": False},
                            },
                            "status": {"deprecated": False},
                        },
                        # nested sub-feature
                        "none": {
                            "__compat": {
                                "support": {
                                    "chrome": {
                                        "version_added": None,
                                        "notes": ["a", "b"],
                                    }
                                }
                            }
                        },
                    }
                }
            }
        }
        (data / "css" / "properties" / "grid.json").write_text(json.dumps(grid))

    return data


@pytest.fixture
def db(tmp_path):
    data = write_data_dir(tmp_path)
    db_path = tmp_path / "out.db"
    build_db.build(data, db_path)
    return sqlite_utils.Database(db_path)


# ---------------------------------------------------------------------------
# Schema / table tests
# ---------------------------------------------------------------------------


def test_tables_created(db):
    expected = {
        "browsers",
        "browser_releases",
        "features",
        "feature_tags",
        "feature_spec_urls",
        "support",
        "support_flags",
        "metadata",
    }
    assert expected.issubset(set(db.table_names()))


def test_browsers_and_releases(db):
    chrome = db["browsers"].get("chrome")
    assert chrome["name"] == "Chrome"
    assert chrome["type"] == "desktop"
    assert db["browsers"].get("chrome_android")["upstream"] == "chrome"
    assert db["browser_releases"].count == 2  # chrome 1 + 57


def test_feature_hierarchy_and_paths(db):
    flex = db["features"].get("css.properties.flex")
    assert flex["category"] == "css"
    assert flex["name"] == "flex"
    assert flex["standard_track"] == 1
    # nested feature points at its parent feature
    none = db["features"].get("css.properties.grid.none")
    assert none["parent_id"] == "css.properties.grid"
    assert none["depth"] == 4


def test_spec_urls_and_tags(db):
    specs = [
        r["spec_url"]
        for r in db["feature_spec_urls"].rows_where("feature_id = ?", ["css.properties.flex"])
    ]
    assert sorted(specs) == ["https://spec/a", "https://spec/b"]
    tags = [r["tag"] for r in db["feature_tags"].rows_where("feature_id = ?", ["css.properties.flex"])]
    assert tags == ["web-features:flexbox"]


def test_support_normalization(db):
    # plain version string
    row = db["support"].get(("css.properties.flex", "chrome", 0))
    assert row["version_added"] == "29"
    assert row["supported"] == 1
    assert row["is_mirror"] == 0

    # mirror entry
    mirror = db["support"].get(("css.properties.flex", "chrome_android", 0))
    assert mirror["is_mirror"] == 1
    assert mirror["version_added"] is None
    assert mirror["supported"] is None


def test_support_boolean_and_null(db):
    # version_added: False -> supported 0
    not_supported = db["support"].get(("css.properties.grid", "chrome_android", 0))
    assert not_supported["supported"] == 0
    assert not_supported["version_added"] is None

    # version_added: True -> supported 1, version unknown (statement index 1)
    true_added = db["support"].get(("css.properties.grid", "chrome", 1))
    assert true_added["supported"] == 1
    assert true_added["version_added"] is None
    assert true_added["version_removed"] == "10"

    # version_added: null -> supported unknown, notes list joined
    unknown = db["support"].get(("css.properties.grid.none", "chrome", 0))
    assert unknown["supported"] is None
    assert unknown["notes"] == "a\nb"


def test_support_array_indexes(db):
    rows = list(db["support"].rows_where("feature_id = ? and browser_id = ?", ["css.properties.grid", "chrome"]))
    assert {r["statement_index"] for r in rows} == {0, 1}


def test_flags(db):
    flag = db["support_flags"].get(("css.properties.grid", "chrome", 1, 0))
    assert flag["type"] == "preference"
    assert flag["name"] == "enable-grid"
    assert flag["value_to_set"] == "true"


def test_metadata(db):
    meta = {r["key"]: r["value"] for r in db["metadata"].rows}
    assert meta["bcd_version"] == "9.9.9"
    assert "imported_at" in meta


# ---------------------------------------------------------------------------
# Refresh / idempotency
# ---------------------------------------------------------------------------


def test_refresh_is_idempotent_and_prunes(tmp_path):
    # First build with the grid feature present.
    data = write_data_dir(tmp_path, with_grid=True)
    db_path = tmp_path / "out.db"
    build_db.build(data, db_path)
    db = sqlite_utils.Database(db_path)
    with_grid = db["features"].count
    assert db["features"].count_where("id = ?", ["css.properties.grid"]) == 1

    # Rebuild identically -> counts unchanged (idempotent).
    build_db.build(data, db_path)
    assert sqlite_utils.Database(db_path)["features"].count == with_grid

    # Now refresh from a source without grid -> grid + children pruned.
    data2 = write_data_dir(tmp_path / "v2", with_grid=False)
    build_db.build(data2, db_path)
    db = sqlite_utils.Database(db_path)
    assert db["features"].count_where("id = ?", ["css.properties.grid"]) == 0
    assert db["features"].count_where("id = ?", ["css.properties.grid.none"]) == 0
    # related support rows pruned too
    assert db["support"].count_where("feature_id = ?", ["css.properties.grid"]) == 0
    # untouched feature survives
    assert db["features"].count_where("id = ?", ["css.properties.flex"]) == 1


def test_foreign_key_integrity(db):
    assert list(db.conn.execute("PRAGMA foreign_key_check")) == []


# ---------------------------------------------------------------------------
# Schema-drift detection via Pydantic
# ---------------------------------------------------------------------------


def test_unknown_compat_field_rejected():
    with pytest.raises(ValidationError):
        CompatStatement.model_validate({"support": {}, "brand_new_field": 1})


def test_unknown_browser_field_rejected():
    with pytest.raises(ValidationError):
        BrowsersFile.model_validate(
            {"browsers": {"x": {"name": "X", "type": "desktop", "surprise": 1}}}
        )


def test_invalid_browser_type_rejected():
    with pytest.raises(ValidationError):
        BrowsersFile.model_validate(
            {"browsers": {"x": {"name": "X", "type": "quantum"}}}
        )
