"""Load every tab of a Google Spreadsheet into BigQuery with SCD2 versioning.

One HTTP-triggered Cloud Function handles a whole file. Tabs are discovered at
runtime, so adding a sheet to the spreadsheet needs a config entry at most -
never a code change or a redeploy.

    POST /                      load every tab the config allows
    POST /?tabs=Customer,Orders load just those tabs
    POST /?dry_run=1            read, type and validate; write nothing

Layout in BigQuery (deliberately not a medallion):

    core.<table>        SCD2 history, append-only, never truncated
    core.v_<table>      current rows only - what people and BI tools read
    core.v_<table>_history  every version with its validity window
    staging.<table>__stg    this run's input; rebuilt each run, expires in 1 day
    ops.load_runs       one row per tab per run
    ops.schema_changes  one row per column added or widened

There is no bronze/silver/gold split. With versioned tables and a current-view
in front of each, a raw layer would only hold a lossier copy of what `_raw`
already carries on every row.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import traceback
import uuid
from typing import Any, Dict, List, Optional, Sequence

try:
    import functions_framework
except ImportError:  # pragma: no cover - only missing outside Cloud Functions
    # The framework is provided by the runtime. Stubbing it here lets the whole
    # module - including process_tab and reconcile_schema - be imported and
    # tested locally with no cloud dependencies installed.
    class _Stub(object):
        @staticmethod
        def http(fn):
            return fn

    functions_framework = _Stub()  # type: ignore[assignment]

import scd2_ops as ops
import scd2_sheets as sheets
import scd2_sql as sql
from scd2_bq import STAGING_EXPIRATION_MS, BigQuery
from scd2_config import MODE_SCD2, MODE_SNAPSHOT, ON_DUP_KEEP_LAST, load_config
from scd2_schema import infer_schema, normalise_headers, resolve_type
from scd2_transform import build_records, find_duplicate_keys, missing_key_rows

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sheets_multi_scd2")

#: How many duplicate keys to name in an error message before truncating.
MAX_REPORTED = 10


# ---------------------------------------------------------------------------
# Schema reconciliation
# ---------------------------------------------------------------------------


def reconcile_schema(bq, dataset, table, inferred, key_columns, result):
    """Align the inferred schema with the table that already exists.

    Three rules, in priority order:

    1. A column in the sheet but not the table is added. Nothing else changes.
    2. A column whose type changed is widened only if BigQuery can widen it in
       place; otherwise the existing type stands and values that will not cast
       land in `_raw` with a counter in the ops log.
    3. A column in the table but no longer in the sheet is left alone. Dropping
       it would destroy history that is still valid for older versions.

    Returns the schema to use for this load.
    """
    existing = bq.table_schema(dataset, table)
    table_ref = sql.qualify(bq.project, dataset, table)

    if existing is None:
        ddl = sql.create_scd2_table_ddl(table_ref, inferred, key_columns)
        bq.execute(ddl)
        result.schema_changes.append(
            {"change_type": "table_created", "column_name": None,
             "from_type": None, "to_type": None}
        )
        return dict(inferred)

    source_existing = dict(
        (name, t) for name, t in existing.items() if not name.startswith("_")
    )
    final: Dict[str, str] = {}

    for column, incoming in inferred.items():
        if column not in source_existing:
            bq.execute(sql.add_column_ddl(table_ref, column, incoming))
            result.schema_changes.append(
                {"change_type": "column_added", "column_name": column,
                 "from_type": None, "to_type": incoming}
            )
            final[column] = incoming
            continue

        current = source_existing[column]
        chosen, needs_alter = resolve_type(current, incoming)
        if needs_alter:
            bq.execute(sql.alter_type_ddl(table_ref, column, chosen))
            result.schema_changes.append(
                {"change_type": "type_widened", "column_name": column,
                 "from_type": current, "to_type": chosen}
            )
        elif chosen != incoming:
            result.schema_changes.append(
                {"change_type": "type_conflict_kept", "column_name": column,
                 "from_type": current, "to_type": incoming}
            )
        final[column] = chosen

    for column in source_existing:
        if column not in inferred:
            result.schema_changes.append(
                {"change_type": "column_missing_from_source",
                 "column_name": column, "from_type": source_existing[column],
                 "to_type": None}
            )

    return final


# ---------------------------------------------------------------------------
# One tab
# ---------------------------------------------------------------------------


def process_tab(bq, service, config, tab_config, load_id, run_ts, dry_run=False):
    """Read, validate and version one tab. Never raises - failures become a
    result row so one bad tab cannot take down the other twenty."""
    started = _dt.datetime.utcnow()
    result = ops.TabResult(tab_config.tab, tab_config.table, tab_config.mode)

    try:
        header_cells, rows = sheets.read_tab(
            service, config.spreadsheet_id, tab_config.tab, tab_config.header_row
        )
        if not header_cells:
            return result.skip("tab is empty")

        header = normalise_headers(header_cells)
        inferred = infer_schema(header, rows, tab_config.types)
        for column in tab_config.exclude_columns:
            inferred.pop(column, None)

        data = build_records(
            header, rows, inferred, exclude_columns=tab_config.exclude_columns
        )
        result.rows_read = data.rows_read
        result.rows_skipped = data.rows_skipped
        result.coercion_errors = data.coercion_errors

        if tab_config.mode == MODE_SCD2:
            blocked = _validate_keys(data, tab_config, result)
            if blocked:
                return blocked

        if not data.records:
            # An empty tab in SCD2 mode is ambiguous: it could mean "everything
            # was deleted" or "somebody cleared it by accident". Refusing is
            # the reversible choice - soft-deleting every row is not.
            if tab_config.mode == MODE_SCD2:
                return result.block(
                    "tab has a header but no data rows; refusing to soft-delete "
                    "every existing row. Set mode: snapshot if an empty tab is "
                    "expected here."
                )
            return result.skip("no data rows")

        if dry_run:
            result.rows_staged = len(data.records)
            return result.skip(
                "dry run: %d rows would be loaded into %s"
                % (len(data.records), tab_config.table)
            )

        final_schema = reconcile_schema(
            bq, config.dataset, tab_config.table, inferred,
            tab_config.business_key, result,
        )
        _ensure_table(bq, config, tab_config, final_schema)

        staging_table = "%s__stg" % tab_config.table
        result.rows_staged = bq.load_staging(
            config.staging_dataset, staging_table, data.records, final_schema
        )

        target_ref = sql.qualify(bq.project, config.dataset, tab_config.table)
        staging_ref = sql.qualify(bq.project, config.staging_dataset, staging_table)
        params = {"run_ts": run_ts, "load_id": load_id, "source_tab": tab_config.tab}

        if tab_config.mode == MODE_SCD2:
            bq.execute(
                sql.scd2_merge_script(
                    target_ref, staging_ref,
                    tab_config.business_key, list(final_schema.keys()),
                ),
                params,
            )
            _count_scd2(bq, target_ref, run_ts, result)
        else:
            bq.execute(
                sql.snapshot_insert_script(
                    target_ref, staging_ref, list(final_schema.keys())
                ),
                params,
            )
            result.rows_inserted = result.rows_staged

        if config.create_views:
            _ensure_views(bq, config, tab_config, final_schema)

    except Exception as exc:  # noqa: BLE001 - deliberate: isolate tab failures
        log.exception("tab %s failed", tab_config.tab)
        result.fail("%s: %s" % (type(exc).__name__, exc))

    finally:
        delta = _dt.datetime.utcnow() - started
        result.duration_ms = int(delta.total_seconds() * 1000)

    return result


def _validate_keys(data, tab_config, result):
    """Refuse a tab whose business keys cannot support versioning."""
    blank = missing_key_rows(data.records, tab_config.business_key)
    if blank:
        return result.block(
            "%d row(s) have an empty business key (sheet rows %s); a NULL key "
            "cannot be matched, so every run would insert a new version"
            % (len(blank), _sample(blank))
        )

    duplicates = find_duplicate_keys(data.records, tab_config.business_key)
    if duplicates:
        if tab_config.on_duplicate_key == ON_DUP_KEEP_LAST:
            keep = {}
            for record in data.records:
                keep[tuple(record.get(c) for c in tab_config.business_key)] = record
            data.records = list(keep.values())
            result.message = (
                "%d duplicate key(s) collapsed to the last occurrence"
                % len(duplicates)
            )
        else:
            detail = "; ".join(
                "%s at rows %s" % (list(key), rows)
                for key, rows in duplicates[:MAX_REPORTED]
            )
            return result.block(
                "%d duplicate business key(s): %s. Fix the sheet, or set "
                "on_duplicate_key: keep_last if later rows are meant to win."
                % (len(duplicates), detail)
            )
    return None


def _ensure_table(bq, config, tab_config, schema):
    """Create the target table when reconcile_schema did not already."""
    table_ref = sql.qualify(bq.project, config.dataset, tab_config.table)
    if tab_config.mode == MODE_SNAPSHOT:
        if bq.table_schema(config.dataset, tab_config.table) is None:
            bq.execute(sql.create_snapshot_table_ddl(table_ref, schema))


def _count_scd2(bq, target_ref, run_ts, result):
    """Count what the merge actually did, from the table itself.

    Read back rather than inferred from the staging row count: the numbers in
    the ops log should describe what is in the table, not what was intended.
    """
    counts = bq.query_rows(
        "SELECT\n"
        "  COUNTIF(`_valid_from` = @run_ts) AS inserted,\n"
        "  COUNTIF(`_valid_to` = @run_ts AND NOT `_is_deleted`) AS closed,\n"
        "  COUNTIF(`_valid_to` = @run_ts AND `_is_deleted`) AS deleted\n"
        "FROM %s\n"
        "WHERE `_valid_from` = @run_ts OR `_valid_to` = @run_ts" % target_ref,
        {"run_ts": run_ts},
    )
    if counts:
        result.rows_inserted = counts[0].get("inserted") or 0
        result.rows_closed = counts[0].get("closed") or 0
        result.rows_deleted = counts[0].get("deleted") or 0


def _ensure_views(bq, config, tab_config, schema):
    if tab_config.mode != MODE_SCD2:
        return
    table_ref = sql.qualify(bq.project, config.dataset, tab_config.table)
    current = sql.qualify(bq.project, config.dataset, "v_%s" % tab_config.table)
    history = sql.qualify(
        bq.project, config.dataset, "v_%s_history" % tab_config.table
    )
    bq.execute(sql.create_current_view_ddl(current, table_ref, list(schema.keys())))
    bq.execute(sql.create_history_view_ddl(history, table_ref))


def _sample(values, limit=MAX_REPORTED):
    shown = list(values)[:limit]
    suffix = "" if len(values) <= limit else " (+%d more)" % (len(values) - limit)
    return "%s%s" % (shown, suffix)


# ---------------------------------------------------------------------------
# Tab selection
# ---------------------------------------------------------------------------


def resolve_tabs(config, service, only: Optional[Sequence[str]] = None):
    """Decide which tabs to process this run.

    Declared tabs always run. Undeclared tabs run only when `discover_tabs` is
    on, and then in snapshot mode - a discovered tab has no business key, and
    guessing one would quietly produce a wrong version history.
    """
    titles: List[str] = []
    if config.discover_tabs:
        titles = sheets.list_tabs(service, config.spreadsheet_id)
    else:
        titles = list(config.tabs.keys())

    for declared in config.tabs:
        if declared not in titles:
            titles.append(declared)

    selected = []
    for title in titles:
        if only and title not in only:
            continue
        if config.is_excluded(title):
            continue
        selected.append(config.tab_config(title))
    return selected


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@functions_framework.http
def load_spreadsheet(request):
    load_id = uuid.uuid4().hex
    run_ts = _dt.datetime.utcnow().replace(microsecond=0)
    only = None
    dry_run = False

    if request is not None:
        raw = request.args.get("tabs") if hasattr(request, "args") else None
        if raw:
            only = [t.strip() for t in raw.split(",") if t.strip()]
        dry_run = str(request.args.get("dry_run", "")).lower() in ("1", "true", "yes")

    try:
        config = load_config()
        service = sheets.build_service()
        bq = BigQuery(project=config.project, location=config.location)

        bq.ensure_dataset(config.dataset)
        bq.ensure_dataset(config.ops_dataset)
        bq.ensure_dataset(
            config.staging_dataset,
            default_table_expiration_ms=STAGING_EXPIRATION_MS,
        )
        bq.execute(ops.run_log_ddl(bq.project, config.ops_dataset))
        bq.execute(ops.schema_log_ddl(bq.project, config.ops_dataset))

        tabs = resolve_tabs(config, service, only)
        log.info("load_id=%s processing %d tab(s)", load_id, len(tabs))

        results = []
        for tab_config in tabs:
            results.append(
                process_tab(bq, service, config, tab_config, load_id, run_ts, dry_run)
            )

        if not dry_run:
            _write_ops(bq, config, results, load_id, run_ts)

        status = ops.overall_status(results)
        body = {
            "load_id": load_id,
            "run_ts": run_ts.isoformat() + "Z",
            "status": status,
            "tabs": [r.to_summary() for r in results],
        }
        log.info("load_id=%s status=%s", load_id, status)
        return (json.dumps(body, ensure_ascii=False), _http_code(status),
                {"Content-Type": "application/json; charset=utf-8"})

    except Exception as exc:  # noqa: BLE001 - top level, must return not raise
        log.exception("run failed before any tab was processed")
        body = {
            "load_id": load_id,
            "status": "error",
            "error": "%s: %s" % (type(exc).__name__, exc),
            "traceback": traceback.format_exc()[-2000:],
        }
        return (json.dumps(body, ensure_ascii=False), 500,
                {"Content-Type": "application/json; charset=utf-8"})


def _write_ops(bq, config, results, load_id, run_ts):
    """Write the ops rows. A logging failure must not mask the load result."""
    try:
        rows = [r.to_row(load_id, run_ts, config.spreadsheet_id) for r in results]
        if rows:
            bq.insert_rows(config.ops_dataset, ops.RUN_LOG, rows)

        changes = []
        for result in results:
            for change in result.schema_changes:
                entry = dict(change)
                entry.update(
                    {"load_id": load_id, "run_ts": run_ts.isoformat(),
                     "target_table": result.table}
                )
                changes.append(entry)
        if changes:
            bq.insert_rows(config.ops_dataset, ops.SCHEMA_LOG, changes)
    except Exception:  # noqa: BLE001
        log.exception("failed to write ops log for load_id=%s", load_id)


def _http_code(status):
    """Non-2xx when something needs attention, so Scheduler and Monitoring see
    a failed invocation rather than a green tick with bad news in the body."""
    if status == ops.STATUS_OK:
        return 200
    if status == "partial":
        return 207
    return 500
