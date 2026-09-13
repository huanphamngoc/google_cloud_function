"""Orchestrate the Sheets -> BigQuery SCD2 load from Cloud Composer.

Why Airflow rather than one Cloud Scheduler call
------------------------------------------------
The function can already load every tab in a single invocation. What that
cannot give you is per-tab visibility: one HTTP call is one green or red dot,
and a tab blocked for duplicate keys looks the same as a network blip.

This DAG fans out to one task per declared tab, so the Airflow UI shows
exactly which sheet failed, retries only that sheet, and lets the rest of the
load finish. It also separates the three things that fail for different
reasons and deserve different responses:

    preflight  -> the data is wrong (duplicate keys, empty business key)
    load_<tab> -> the load is wrong (API error, timeout, IAM)
    verify_<t> -> the RESULT is wrong (a key ended up with two current rows)

The tab list is read from the same `sources.yaml` the function uses, uploaded
alongside this file. Adding a tab means editing that one file - the DAG picks
it up on the next parse, with no code change here.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, Dict, List

import pendulum
import yaml
from airflow import DAG
from airflow.decorators import task
from airflow.exceptions import AirflowFailException, AirflowSkipException
from airflow.operators.empty import EmptyOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryCheckOperator
from airflow.utils.trigger_rule import TriggerRule

from scd2_dag_helpers import (
    OUTCOME_FAIL,
    OUTCOME_RETRY,
    OUTCOME_SKIP,
    classify_response,
    one_current_row_sql,
    parse_tabs,
    task_id,
)

log = logging.getLogger(__name__)

# --- settings, all injected as Composer environment variables --------------
FUNCTION_URL = os.environ.get("SHEETS_FUNCTION_URL", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "elt-drive-to-bq")
BQ_DATASET = os.environ.get("BQ_DATASET", "core")
BQ_OPS_DATASET = os.environ.get("BQ_OPS_DATASET", "ops")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "asia-southeast1")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sources.yaml")
LOCAL_TZ = pendulum.timezone("Asia/Ho_Chi_Minh")

#: The function walks one tab in well under a minute at this scale, but the
#: HTTP call still has to outlive a cold start plus the Sheets read.
REQUEST_TIMEOUT = 540


# ---------------------------------------------------------------------------
# Calling the function
# ---------------------------------------------------------------------------


def _identity_token(audience: str) -> str:
    """Mint an OIDC token for the function.

    The Composer worker has no key file - this reads the environment's own
    identity from the metadata server, which is why nothing here needs a
    secret and nothing can leak one.
    """
    import google.auth.transport.requests
    from google.oauth2 import id_token

    return id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience
    )


def _invoke(tabs: List[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """POST to the loader and turn its reply into a task outcome.

    The distinction that matters for Airflow:

    - `blocked` means the spreadsheet is wrong (duplicate key, empty key, a
      tab cleared by accident). Retrying cannot fix that, so it raises
      AirflowFailException and the task stops immediately instead of burning
      three retries and a half hour before telling anyone.
    - anything else - a 5xx, a timeout, a transient Sheets error - raises a
      normal exception and is retried.
    """
    import requests

    if not FUNCTION_URL:
        raise AirflowFailException(
            "SHEETS_FUNCTION_URL is not set on the Composer environment"
        )

    params = {}
    if tabs:
        params["tabs"] = ",".join(tabs)
    if dry_run:
        params["dry_run"] = "1"

    response = requests.post(
        FUNCTION_URL,
        params=params,
        headers={"Authorization": "Bearer %s" % _identity_token(FUNCTION_URL)},
        timeout=REQUEST_TIMEOUT,
    )

    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(
            "loader returned non-JSON (HTTP %s): %s"
            % (response.status_code, response.text[:500])
        )

    log.info("loader replied: %s", json.dumps(body, ensure_ascii=False)[:2000])

    # A dry run reports every tab as skipped by design; treating that as a
    # skip would mark preflight skipped and take the whole DAG with it.
    outcome, message = classify_response(
        body, response.status_code, allow_skip=not dry_run
    )
    if outcome == OUTCOME_FAIL:
        raise AirflowFailException("tab refused, fix the spreadsheet - %s" % message)
    if outcome == OUTCOME_RETRY:
        raise RuntimeError(message)
    if outcome == OUTCOME_SKIP:
        raise AirflowSkipException(message)

    return body


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_tabs() -> List[Dict[str, Any]]:
    """Read the declared tabs from the config shipped next to this DAG.

    A missing or malformed config must not leave a broken DAG in the UI, so it
    degrades to a single task that loads everything - the same behaviour as
    calling the function directly.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (IOError, OSError, yaml.YAMLError) as exc:
        log.warning("cannot read %s (%s); falling back to a single load task",
                    CONFIG_PATH, exc)
        return []
    return parse_tabs(data)


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

default_args = {
    "owner": "data",
    "retries": 2,
    "retry_delay": dt.timedelta(minutes=5),
    "email": [ALERT_EMAIL] if ALERT_EMAIL else [],
    "email_on_failure": bool(ALERT_EMAIL),
    "email_on_retry": False,
}

with DAG(
    dag_id="sheets_to_bq_scd2",
    description="Load every declared tab of the spreadsheet into BigQuery with SCD2 versioning",
    default_args=default_args,
    schedule="0 6 * * 1-5",
    start_date=pendulum.datetime(2026, 9, 1, tz=LOCAL_TZ),
    catchup=False,
    max_active_runs=1,
    tags=["sheets", "bigquery", "scd2"],
    doc_md=__doc__,
) as dag:

    start = EmptyOperator(task_id="start")

    @task(task_id="preflight")
    def preflight():
        """Dry run: read, type and validate every tab, write nothing.

        Runs first so a duplicate key is reported before any table is touched.
        Without it, tab 1 would load, tab 7 would be refused, and the run would
        end half-applied - technically fine for SCD2, but confusing to explain
        the next morning.
        """
        body = _invoke(dry_run=True)
        total = sum(t.get("rows_read", 0) for t in body.get("tabs", []))
        log.info("preflight ok: %d tab(s), %d row(s) would load",
                 len(body.get("tabs", [])), total)
        return {"tabs": len(body.get("tabs", [])), "rows": total}

    @task(task_id="report", trigger_rule=TriggerRule.ALL_DONE)
    def report():
        """Summarise the run from the ops log, whatever happened upstream.

        ALL_DONE, not ALL_SUCCESS: the run summary is most valuable precisely
        when something failed.
        """
        from google.cloud import bigquery

        client = bigquery.Client(project=GCP_PROJECT)
        rows = list(
            client.query(
                "SELECT tab, status, rows_read, rows_inserted, rows_closed,"
                "       rows_deleted, message\n"
                "FROM `%s.%s.load_runs`\n"
                "WHERE run_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)\n"
                "ORDER BY run_ts DESC" % (GCP_PROJECT, BQ_OPS_DATASET),
                location=BQ_LOCATION,
            ).result()
        )
        if not rows:
            log.warning("no ops rows in the last 6 hours - did the loader run?")
            return {}

        for row in rows:
            log.info(
                "%-20s %-8s read=%-5s ins=%-5s closed=%-5s deleted=%-5s %s",
                row.tab, row.status, row.rows_read, row.rows_inserted,
                row.rows_closed, row.rows_deleted, row.message or "",
            )
        bad = [r.tab for r in rows if r.status not in ("ok", "skipped")]
        if bad:
            log.error("tabs needing attention: %s", bad)
        return {"tabs": len(rows), "needs_attention": bad}

    finish = EmptyOperator(task_id="finish", trigger_rule=TriggerRule.ALL_DONE)

    preflight_task = preflight()
    report_task = report()
    start >> preflight_task

    declared = _load_tabs()

    if not declared:
        # No usable config: fall back to one task that loads everything, so the
        # DAG still works rather than showing up empty in the UI.
        @task(task_id="load_all_tabs")
        def load_all():
            return _invoke()

        preflight_task >> load_all() >> report_task >> finish

    else:
        for entry in declared:
            tab, table, mode = entry["tab"], entry["table"], entry["mode"]
            suffix = task_id(table)

            @task(task_id="load_%s" % suffix)
            def load_tab(tab_title=tab):
                # _invoke already raises: AirflowFailException for a refused
                # tab, AirflowSkipException for an empty one, RuntimeError for
                # anything retryable.
                body = _invoke(tabs=[tab_title])
                return next(
                    (t for t in body.get("tabs", []) if t.get("tab") == tab_title), {}
                )

            load_task = load_tab()
            preflight_task >> load_task

            if mode == "scd2" and entry["business_key"]:
                check = BigQueryCheckOperator(
                    task_id="verify_%s" % suffix,
                    sql=one_current_row_sql(
                        GCP_PROJECT, BQ_DATASET, table, entry["business_key"]
                    ),
                    use_legacy_sql=False,
                    location=BQ_LOCATION,
                    retries=0,  # a broken invariant will not fix itself
                )
                load_task >> check >> report_task
            else:
                load_task >> report_task

        report_task >> finish
