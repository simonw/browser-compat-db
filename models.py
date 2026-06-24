"""Pydantic models mirroring the mdn/browser-compat-data JSON format.

These models are intentionally strict (``extra="forbid"``) so that the import
script raises a ``ValidationError`` if MDN ever adds or renames a field. That
turns a silent schema drift into a loud, actionable failure.

The shapes here follow ``schemas/compat-data.schema.json`` and
``schemas/browsers.schema.json`` in the upstream repository.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Base model that rejects unknown keys to catch upstream schema drift."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Browser metadata (browsers/*.json)
# ---------------------------------------------------------------------------


class BrowserRelease(Strict):
    release_date: Optional[str] = None
    release_notes: Optional[str] = None
    status: Literal[
        "retired", "current", "beta", "nightly", "planned", "esr"
    ]
    engine: Optional[str] = None
    engine_version: Optional[str] = None


class Browser(Strict):
    name: str
    type: Literal["desktop", "mobile", "server", "xr"]
    upstream: Optional[str] = None
    preview_name: Optional[str] = None
    pref_url: Optional[str] = None
    accepts_flags: Optional[bool] = None
    accepts_webextensions: Optional[bool] = None
    releases: Dict[str, BrowserRelease] = Field(default_factory=dict)


class BrowsersFile(Strict):
    """Top level of a browsers/<name>.json file: ``{"browsers": {...}}``."""

    browsers: Dict[str, Browser]


# ---------------------------------------------------------------------------
# Compat data (api/, css/, ... nested *.json files)
# ---------------------------------------------------------------------------


class FlagStatement(Strict):
    type: Literal["preference", "runtime_flag"]
    name: str
    value_to_set: Optional[str] = None


class SimpleSupportStatement(Strict):
    # version_added/version_removed are a version string ("57", "≤10",
    # "preview"), a bool, or null when unknown.
    version_added: Union[str, bool, None] = None
    version_removed: Union[str, bool, None] = None
    prefix: Optional[str] = None
    alternative_name: Optional[str] = None
    flags: Optional[List[FlagStatement]] = None
    impl_url: Union[str, List[str], None] = None
    partial_implementation: Optional[bool] = None
    # notes may be a single string or a list of strings.
    notes: Union[str, List[str], None] = None


# A support entry per browser is either the literal string "mirror", a single
# support statement, or an array mixing the two.
SupportEntry = Union[
    Literal["mirror"],
    SimpleSupportStatement,
    List[Union[Literal["mirror"], SimpleSupportStatement]],
]


class StatusBlock(Strict):
    experimental: Optional[bool] = None
    standard_track: Optional[bool] = None
    deprecated: Optional[bool] = None


class CompatStatement(Strict):
    description: Optional[str] = None
    mdn_url: Optional[str] = None
    spec_url: Union[str, List[str], None] = None
    tags: Optional[List[str]] = None
    support: Dict[str, SupportEntry] = Field(default_factory=dict)
    status: Optional[StatusBlock] = None
