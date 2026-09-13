"""Configuration loading and validation.

The whole point of this package is that adding a tab, or a whole new
spreadsheet, is a config edit rather than a code change. That only holds if
the config is validated properly - a typo in a business key would otherwise
surface as a corrupted version history weeks later.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from scd2_schema import VALID_TYPES, normalise_column

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sources.yaml")

MODE_SCD2 = "scd2"
MODE_SNAPSHOT = "snapshot"
VALID_MODES = frozenset({MODE_SCD2, MODE_SNAPSHOT})

ON_DUP_BLOCK = "block"
ON_DUP_KEEP_LAST = "keep_last"
VALID_ON_DUPLICATE = frozenset({ON_DUP_BLOCK, ON_DUP_KEEP_LAST})

_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")


class ConfigError(ValueError):
    """Raised for any configuration problem, with the offending key named."""


class TabConfig(object):
    """Resolved settings for one tab of one spreadsheet."""

    def __init__(
        self,
        tab,
        table,
        mode=MODE_SCD2,
        business_key=(),
        header_row=1,
        types=None,
        exclude_columns=(),
        on_duplicate_key=ON_DUP_BLOCK,
        configured=True,
    ):
        self.tab = tab
        self.table = table
        self.mode = mode
        self.business_key = tuple(business_key)
        self.header_row = header_row
        self.types = dict(types or {})
        self.exclude_columns = tuple(exclude_columns)
        self.on_duplicate_key = on_duplicate_key
        #: False when the tab was discovered rather than declared - it loads in
        #: snapshot mode and is reported so somebody can give it a key.
        self.configured = configured

    def __repr__(self):  # pragma: no cover - debugging aid
        return "TabConfig(tab=%r, table=%r, mode=%r, key=%r)" % (
            self.tab,
            self.table,
            self.mode,
            self.business_key,
        )


class Config(object):
    """Top-level configuration for one run."""

    def __init__(
        self,
        spreadsheet_id,
        project=None,
        dataset="core",
        staging_dataset="staging",
        ops_dataset="ops",
        location="asia-southeast1",
        discover_tabs=True,
        exclude_tabs=(),
        tabs=None,
        table_prefix="",
        create_views=True,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.project = project
        self.dataset = dataset
        self.staging_dataset = staging_dataset
        self.ops_dataset = ops_dataset
        self.location = location
        self.discover_tabs = discover_tabs
        self.exclude_tabs = tuple(exclude_tabs)
        self.tabs = dict(tabs or {})
        self.table_prefix = table_prefix
        self.create_views = create_views

    def is_excluded(self, tab_title):
        """Match a tab title against the exclusion patterns (regex, full match
        anywhere in the title). Declared tabs always win over exclusions."""
        if tab_title in self.tabs:
            return False
        for pattern in self.exclude_tabs:
            try:
                if re.search(pattern, tab_title):
                    return True
            except re.error:
                # A malformed pattern should not take the whole run down; fall
                # back to a literal comparison and let validation flag it.
                if pattern == tab_title:
                    return True
        return False

    def tab_config(self, tab_title):
        """Resolve settings for a tab, whether declared or discovered."""
        declared = self.tabs.get(tab_title)
        if declared is not None:
            return declared
        return TabConfig(
            tab=tab_title,
            table=self.table_prefix + normalise_column(tab_title),
            mode=MODE_SNAPSHOT,
            business_key=(),
            configured=False,
        )


def _require(mapping, key, where):
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError("%s: missing required key %r" % (where, key))
    return mapping[key]


def parse_config(data: Mapping[str, Any]) -> Config:
    """Validate a raw config mapping and resolve it into a `Config`."""
    if not isinstance(data, Mapping):
        raise ConfigError("config root must be a mapping")

    spreadsheet_id = _require(data, "spreadsheet_id", "config")
    table_prefix = data.get("table_prefix", "") or ""

    tabs: Dict[str, TabConfig] = {}
    raw_tabs = data.get("tabs") or {}
    if not isinstance(raw_tabs, Mapping):
        raise ConfigError("config: `tabs` must be a mapping of tab title -> settings")

    for title, raw in raw_tabs.items():
        where = "tabs.%s" % title
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise ConfigError("%s: settings must be a mapping" % where)

        mode = str(raw.get("mode", MODE_SCD2)).lower()
        if mode not in VALID_MODES:
            raise ConfigError(
                "%s: mode must be one of %s, got %r"
                % (where, sorted(VALID_MODES), mode)
            )

        key_raw = raw.get("business_key") or ()
        if isinstance(key_raw, str):
            key_raw = [key_raw]
        business_key = tuple(normalise_column(k) for k in key_raw)

        if mode == MODE_SCD2 and not business_key:
            raise ConfigError(
                "%s: mode 'scd2' requires `business_key` - versioning without a "
                "key cannot tell an edit from a delete plus an insert" % where
            )

        table = raw.get("table") or normalise_column(title)
        table = table_prefix + table
        if not _TABLE_RE.match(table):
            raise ConfigError("%s: resolved table name %r is not valid" % (where, table))

        types = {}
        for column, bq_type in (raw.get("types") or {}).items():
            bq_type = str(bq_type).upper()
            if bq_type not in VALID_TYPES:
                raise ConfigError(
                    "%s.types.%s: unknown BigQuery type %r (allowed: %s)"
                    % (where, column, bq_type, sorted(VALID_TYPES))
                )
            types[normalise_column(column)] = bq_type

        header_row = int(raw.get("header_row", 1))
        if header_row < 1:
            raise ConfigError("%s: header_row must be >= 1" % where)

        on_duplicate = str(raw.get("on_duplicate_key", ON_DUP_BLOCK)).lower()
        if on_duplicate not in VALID_ON_DUPLICATE:
            raise ConfigError(
                "%s: on_duplicate_key must be one of %s"
                % (where, sorted(VALID_ON_DUPLICATE))
            )

        exclude_columns = tuple(
            normalise_column(c) for c in (raw.get("exclude_columns") or ())
        )

        overlap = set(business_key) & set(exclude_columns)
        if overlap:
            raise ConfigError(
                "%s: column(s) %s are both a business key and excluded"
                % (where, sorted(overlap))
            )

        tabs[title] = TabConfig(
            tab=title,
            table=table,
            mode=mode,
            business_key=business_key,
            header_row=header_row,
            types=types,
            exclude_columns=exclude_columns,
            on_duplicate_key=on_duplicate,
            configured=True,
        )

    duplicates = _duplicate_tables(tabs)
    if duplicates:
        raise ConfigError(
            "config: two tabs resolve to the same table name: %s" % duplicates
        )

    return Config(
        spreadsheet_id=spreadsheet_id,
        project=data.get("project"),
        dataset=data.get("dataset", "core"),
        staging_dataset=data.get("staging_dataset", "staging"),
        ops_dataset=data.get("ops_dataset", "ops"),
        location=data.get("location", "asia-southeast1"),
        discover_tabs=bool(data.get("discover_tabs", True)),
        exclude_tabs=tuple(data.get("exclude_tabs") or ()),
        tabs=tabs,
        table_prefix=table_prefix,
        create_views=bool(data.get("create_views", True)),
    )


def _duplicate_tables(tabs: Mapping[str, TabConfig]) -> List[str]:
    seen: Dict[str, str] = {}
    clashes: List[str] = []
    for title, tab in tabs.items():
        if tab.table in seen:
            clashes.append("%r and %r -> %s" % (seen[tab.table], title, tab.table))
        else:
            seen[tab.table] = title
    return clashes


def load_config(path: Optional[str] = None) -> Config:
    """Read and validate the YAML config from disk."""
    import yaml  # imported here so pure-logic tests need no third-party deps

    path = path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    config = parse_config(data)

    # Environment wins over the file: the same image can then run against a dev
    # spreadsheet without editing anything that is committed.
    config.spreadsheet_id = os.environ.get("SPREADSHEET_ID", config.spreadsheet_id)
    config.project = os.environ.get("GCP_PROJECT", config.project) or os.environ.get(
        "GOOGLE_CLOUD_PROJECT", config.project
    )
    config.dataset = os.environ.get("BQ_DATASET", config.dataset)
    config.staging_dataset = os.environ.get("BQ_STAGING_DATASET", config.staging_dataset)
    config.ops_dataset = os.environ.get("BQ_OPS_DATASET", config.ops_dataset)
    config.location = os.environ.get("BQ_LOCATION", config.location)
    return config
