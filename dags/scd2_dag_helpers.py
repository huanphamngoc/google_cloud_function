"""Pure helpers for the SCD2 DAG.

Kept out of the DAG file so they can be tested without Airflow installed. The
DAG imports from here; Composer puts the DAG folder on sys.path, so a plain
module import works.

The interesting one is `classify_response`: deciding whether a loader reply
should retry, fail immediately, or pass is the orchestration logic worth
getting right, and it has nothing to do with Airflow's API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

#: What the DAG should do with a loader reply.
OUTCOME_OK = "ok"
OUTCOME_SKIP = "skip"
OUTCOME_RETRY = "retry"
OUTCOME_FAIL = "fail"


def classify_response(
    body: Dict[str, Any], http_status: int = 200, allow_skip: bool = True
) -> Tuple[str, str]:
    """Map a loader reply to (outcome, message).

    `allow_skip=False` is for the preflight call: a dry run reports every tab
    as skipped by design, and treating that as a skip would mark the preflight
    task skipped and take every downstream task with it.

    The distinction that matters operationally:

    - `blocked` means the spreadsheet is wrong - a duplicate business key, an
      empty key, a tab cleared by accident. No amount of retrying fixes that,
      so it must fail immediately rather than burn three retries and half an
      hour before anyone is told.
    - `error` means the machinery failed - a 5xx, a timeout, a transient
      Sheets error. That is worth retrying.
    - `skipped` means there was nothing to load, which is a normal state for
      a tab created but not yet filled in. It should read as skipped in the
      UI, not as a silent success.
    """
    tabs = body.get("tabs") or []

    blocked = [t for t in tabs if t.get("status") == "blocked"]
    if blocked:
        return OUTCOME_FAIL, "; ".join(
            "%s: %s" % (t.get("tab"), t.get("message", "")) for t in blocked
        )

    failed = [t for t in tabs if t.get("status") == "error"]
    if failed:
        return OUTCOME_RETRY, "; ".join(
            "%s: %s" % (t.get("tab"), t.get("message", "")) for t in failed
        )

    if http_status >= 400:
        return OUTCOME_RETRY, "loader returned HTTP %s" % http_status

    if allow_skip and tabs and all(t.get("status") == "skipped" for t in tabs):
        return OUTCOME_SKIP, "; ".join(
            t.get("message", "nothing to load") for t in tabs
        )

    return OUTCOME_OK, ""


def task_id(name: str) -> str:
    """Airflow task ids allow a restricted character set; tab titles do not."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "tab"


def parse_tabs(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the declared tabs from a parsed `sources.yaml`.

    Deliberately tolerant: a config this cannot read yields an empty list and
    the DAG degrades to one task that loads everything, rather than showing up
    empty in the UI with no explanation.
    """
    if not isinstance(data, dict):
        return []

    tabs = []
    for title, settings in (data.get("tabs") or {}).items():
        settings = settings or {}
        if not isinstance(settings, dict):
            continue
        key = settings.get("business_key") or []
        if isinstance(key, str):
            key = [key]
        tabs.append(
            {
                "tab": title,
                "table": (settings.get("table") or task_id(title)).lower(),
                "mode": str(settings.get("mode", "scd2")).lower(),
                "business_key": [str(k) for k in key],
            }
        )
    return tabs


def one_current_row_sql(
    project: str, dataset: str, table: str, key_columns: List[str]
) -> str:
    """Assert the SCD2 invariant: exactly one current row per business key.

    Runs against the table after the load, so it checks the result rather than
    the intent. If the merge ever regresses, this fails the DAG instead of
    quietly serving doubled totals to a dashboard.

    IFNULL guards the empty-table case: LOGICAL_AND over zero rows is NULL,
    and a NULL would fail the check on a table that is merely new.
    """
    if not key_columns:
        raise ValueError("cannot verify SCD2 without a business key")
    keys = ", ".join("`%s`" % c for c in key_columns)
    return (
        "SELECT IFNULL(LOGICAL_AND(n = 1), TRUE) AS ok FROM (\n"
        "  SELECT %s, COUNTIF(`_is_current`) AS n\n"
        "  FROM `%s.%s.%s`\n"
        "  WHERE NOT `_is_deleted`\n"
        "  GROUP BY %s\n"
        ")" % (keys, project, dataset, table, keys)
    )


def freshness_sql(project: str, ops_dataset: str, max_age_hours: int = 26) -> str:
    """Assert that the ops log has a recent successful run.

    Catches the failure mode no per-task check can see: the DAG did not run at
    all. A green board with no runs on it looks identical to a green board
    with successful runs.
    """
    return (
        "SELECT COUNTIF(status = 'ok') > 0 AS ok\n"
        "FROM `%s.%s.load_runs`\n"
        "WHERE run_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL %d HOUR)"
        % (project, ops_dataset, max_age_hours)
    )
