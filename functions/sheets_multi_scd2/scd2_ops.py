"""The ops log: what ran, when, and what it did.

Every tab of every run writes one row here whether it succeeded or not. That
is what turns "did the pipeline work last night?" into a query instead of a
trip through Cloud Logging.

The DDL lives here rather than in scd2_sql.py because these tables are fixed -
they are the one part of the system whose schema is not driven by a
spreadsheet.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Mapping, Optional, Sequence

RUN_LOG = "load_runs"
SCHEMA_LOG = "schema_changes"

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


def run_log_ddl(project: str, dataset: str) -> str:
    return (
        "CREATE TABLE IF NOT EXISTS `%s.%s.%s` (\n"
        "  load_id STRING,\n"
        "  run_ts TIMESTAMP,\n"
        "  spreadsheet_id STRING,\n"
        "  tab STRING,\n"
        "  target_table STRING,\n"
        "  mode STRING,\n"
        "  status STRING,\n"
        "  rows_read INT64,\n"
        "  rows_staged INT64,\n"
        "  rows_inserted INT64,\n"
        "  rows_closed INT64,\n"
        "  rows_deleted INT64,\n"
        "  rows_skipped INT64,\n"
        "  coercion_errors STRING,\n"
        "  message STRING,\n"
        "  duration_ms INT64\n"
        ")\n"
        "PARTITION BY DATE(run_ts)\n"
        "CLUSTER BY status, tab" % (project, dataset, RUN_LOG)
    )


def schema_log_ddl(project: str, dataset: str) -> str:
    return (
        "CREATE TABLE IF NOT EXISTS `%s.%s.%s` (\n"
        "  load_id STRING,\n"
        "  run_ts TIMESTAMP,\n"
        "  target_table STRING,\n"
        "  change_type STRING,\n"
        "  column_name STRING,\n"
        "  from_type STRING,\n"
        "  to_type STRING\n"
        ")\n"
        "PARTITION BY DATE(run_ts)\n"
        "CLUSTER BY target_table" % (project, dataset, SCHEMA_LOG)
    )


class TabResult(object):
    """Outcome of one tab in one run - the in-memory form of an ops row."""

    def __init__(self, tab: str, table: str, mode: str):
        self.tab = tab
        self.table = table
        self.mode = mode
        self.status = STATUS_OK
        self.rows_read = 0
        self.rows_staged = 0
        self.rows_inserted = 0
        self.rows_closed = 0
        self.rows_deleted = 0
        self.rows_skipped = 0
        self.coercion_errors: Dict[str, int] = {}
        self.message = ""
        self.duration_ms = 0
        self.schema_changes: List[Dict[str, Any]] = []

    def block(self, message: str) -> "TabResult":
        self.status = STATUS_BLOCKED
        self.message = message
        return self

    def fail(self, message: str) -> "TabResult":
        self.status = STATUS_ERROR
        self.message = message
        return self

    def skip(self, message: str) -> "TabResult":
        self.status = STATUS_SKIPPED
        self.message = message
        return self

    def to_row(self, load_id: str, run_ts: _dt.datetime, spreadsheet_id: str) -> dict:
        import json

        return {
            "load_id": load_id,
            "run_ts": run_ts.isoformat(),
            "spreadsheet_id": spreadsheet_id,
            "tab": self.tab,
            "target_table": self.table,
            "mode": self.mode,
            "status": self.status,
            "rows_read": self.rows_read,
            "rows_staged": self.rows_staged,
            "rows_inserted": self.rows_inserted,
            "rows_closed": self.rows_closed,
            "rows_deleted": self.rows_deleted,
            "rows_skipped": self.rows_skipped,
            "coercion_errors": json.dumps(self.coercion_errors, sort_keys=True)
            if self.coercion_errors
            else None,
            "message": self.message[:4000] if self.message else None,
            "duration_ms": self.duration_ms,
        }

    def to_summary(self) -> dict:
        """Compact form returned in the HTTP response."""
        summary = {
            "tab": self.tab,
            "table": self.table,
            "mode": self.mode,
            "status": self.status,
            "rows_read": self.rows_read,
        }
        if self.mode == "scd2":
            summary["inserted"] = self.rows_inserted
            summary["closed"] = self.rows_closed
            summary["deleted"] = self.rows_deleted
        else:
            summary["inserted"] = self.rows_inserted
        if self.coercion_errors:
            summary["coercion_errors"] = self.coercion_errors
        if self.message:
            summary["message"] = self.message
        return summary


def overall_status(results: Sequence[TabResult]) -> str:
    """One word for the whole run, biased towards reporting trouble.

    A run is only `ok` if nothing was blocked or failed. Partial success is
    reported as `partial` rather than `ok`, because a caller that only checks
    for "ok" should notice that a tab was refused.
    """
    if not results:
        return STATUS_OK
    statuses = set(r.status for r in results)
    if statuses <= {STATUS_OK, STATUS_SKIPPED}:
        return STATUS_OK
    if STATUS_OK in statuses:
        return "partial"
    return STATUS_ERROR if STATUS_ERROR in statuses else STATUS_BLOCKED
