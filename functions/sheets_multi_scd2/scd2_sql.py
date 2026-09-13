"""SQL generation for SCD2 tables, views, and the versioning merge.

Pure string building - nothing here executes anything, so the exact SQL that
will hit BigQuery can be asserted in unit tests.

The SCD2 shape
--------------
Every versioned table carries these bookkeeping columns alongside the source
columns:

    _raw         STRING     original row as JSON, including unmapped cells
    _row_hash    STRING     sha256 of the non-null business payload
    _valid_from  TIMESTAMP  when this version became true
    _valid_to    TIMESTAMP  when it stopped; FAR_FUTURE while current
    _is_current  BOOL       exactly one TRUE per business key
    _is_deleted  BOOL       TRUE when the key vanished from the source
    _load_id     STRING     the run that created this version
    _source_tab  STRING     which tab of the spreadsheet it came from

`_valid_to` uses a far-future sentinel rather than NULL so that point-in-time
queries are a plain BETWEEN, with no OR-IS-NULL branch that a reader can
forget and silently miss the current row.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence

FAR_FUTURE = "9999-12-31 23:59:59+00"

#: Bookkeeping columns appended to every SCD2 table, in table order.
SCD2_META: Sequence[tuple] = (
    ("_raw", "STRING"),
    ("_row_hash", "STRING"),
    ("_valid_from", "TIMESTAMP"),
    ("_valid_to", "TIMESTAMP"),
    ("_is_current", "BOOL"),
    ("_is_deleted", "BOOL"),
    ("_load_id", "STRING"),
    ("_source_tab", "STRING"),
)

#: Bookkeeping columns for append-only snapshot tables (tabs with no key).
SNAPSHOT_META: Sequence[tuple] = (
    ("_raw", "STRING"),
    ("_row_hash", "STRING"),
    ("_load_id", "STRING"),
    ("_ingested_at", "TIMESTAMP"),
    ("_source_tab", "STRING"),
)

#: Columns the staging table carries on top of the source columns.
STAGING_META: Sequence[tuple] = (
    ("_raw", "STRING"),
    ("_row_hash", "STRING"),
    ("_source_row", "INT64"),
)


def qualify(project: str, dataset: str, table: str) -> str:
    """Backtick-quote a fully qualified table reference."""
    return "`%s.%s.%s`" % (project, dataset, table)


def _col_list(columns: Sequence[str], prefix: str = "") -> str:
    return ", ".join("%s`%s`" % (prefix, c) for c in columns)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------


def create_scd2_table_ddl(
    table_ref: str,
    columns: Mapping[str, str],
    key_columns: Sequence[str],
) -> str:
    """CREATE TABLE IF NOT EXISTS for a versioned table.

    Partitioned by the date the version opened, clustered on `_is_current`
    plus the first key column: the two predicates essentially every read
    applies. Both are set at creation because adding them later means
    rebuilding the table, and at creation time it is free.
    """
    lines = ["  `%s` %s" % (name, bq_type) for name, bq_type in columns.items()]
    lines += ["  `%s` %s" % (name, bq_type) for name, bq_type in SCD2_META]
    cluster = ["_is_current"]
    if key_columns:
        cluster.append(key_columns[0])
    return (
        "CREATE TABLE IF NOT EXISTS %s (\n%s\n)\n"
        "PARTITION BY DATE(_valid_from)\n"
        "CLUSTER BY %s"
        % (table_ref, ",\n".join(lines), _col_list(cluster))
    )


def create_snapshot_table_ddl(table_ref: str, columns: Mapping[str, str]) -> str:
    """CREATE TABLE IF NOT EXISTS for an append-only snapshot table."""
    lines = ["  `%s` %s" % (name, bq_type) for name, bq_type in columns.items()]
    lines += ["  `%s` %s" % (name, bq_type) for name, bq_type in SNAPSHOT_META]
    return (
        "CREATE TABLE IF NOT EXISTS %s (\n%s\n)\n"
        "PARTITION BY DATE(_ingested_at)\n"
        "CLUSTER BY `_load_id`" % (table_ref, ",\n".join(lines))
    )


def add_column_ddl(table_ref: str, column: str, bq_type: str) -> str:
    return "ALTER TABLE %s ADD COLUMN IF NOT EXISTS `%s` %s" % (
        table_ref,
        column,
        bq_type,
    )


def alter_type_ddl(table_ref: str, column: str, bq_type: str) -> str:
    return "ALTER TABLE %s ALTER COLUMN `%s` SET DATA TYPE %s" % (
        table_ref,
        column,
        bq_type,
    )


def create_current_view_ddl(
    view_ref: str, table_ref: str, columns: Sequence[str]
) -> str:
    """A plain view of the live rows, which is what consumers should read.

    Nobody outside the pipeline should query the versioned table directly: one
    forgotten `_is_current` predicate silently multiplies every count by the
    number of historical versions. The view makes the safe path the default
    one, and keeps reports working if the versioning internals ever change.
    """
    return (
        "CREATE OR REPLACE VIEW %s AS\n"
        "SELECT %s, `_valid_from`, `_load_id`\n"
        "FROM %s\n"
        "WHERE `_is_current` AND NOT `_is_deleted`"
        % (view_ref, _col_list(columns), table_ref)
    )


def create_history_view_ddl(view_ref: str, table_ref: str) -> str:
    """Point-in-time view: every version with an explicit validity window."""
    return (
        "CREATE OR REPLACE VIEW %s AS\n"
        "SELECT *\n"
        "FROM %s" % (view_ref, table_ref)
    )


# ---------------------------------------------------------------------------
# The SCD2 merge
# ---------------------------------------------------------------------------


def _key_join(key_columns: Sequence[str], left: str, right: str) -> str:
    return " AND ".join(
        "%s.`%s` = %s.`%s`" % (left, c, right, c) for c in key_columns
    )


def scd2_merge_script(
    target_ref: str,
    staging_ref: str,
    key_columns: Sequence[str],
    data_columns: Sequence[str],
) -> str:
    """Three statements in one transaction: close changed, close deleted, insert.

    Written as UPDATE + UPDATE + INSERT rather than a single MERGE because a
    MERGE cannot both close the old version and insert the new one for the
    same key in one pass - the row would have to be matched twice.

    Order matters. The two UPDATEs run first, so by the time the INSERT
    evaluates `NOT EXISTS (... _is_current ...)`, a changed key no longer has
    a current row and therefore qualifies for a new version. Unchanged keys
    still have their current row with the same hash and are skipped.

    Wrapped in a transaction so a failure halfway cannot leave a key with zero
    current versions - which would make the row vanish from every report until
    the next successful run.
    """
    if not key_columns:
        raise ValueError("SCD2 requires at least one business key column")

    all_columns = list(data_columns)
    insert_cols = _col_list(all_columns) + ", `_raw`, `_row_hash`, `_valid_from`, `_valid_to`, `_is_current`, `_is_deleted`, `_load_id`, `_source_tab`"
    select_cols = _col_list(all_columns, prefix="s.") + ", s.`_raw`, s.`_row_hash`"

    return """BEGIN TRANSACTION;

-- 1. Close versions whose payload changed.
UPDATE {target} t
SET t.`_valid_to` = @run_ts,
    t.`_is_current` = FALSE
WHERE t.`_is_current`
  AND EXISTS (
    SELECT 1 FROM {staging} s
    WHERE {join_ts} AND s.`_row_hash` != t.`_row_hash`
  );

-- 2. Close versions whose key disappeared from the source (soft delete).
UPDATE {target} t
SET t.`_valid_to` = @run_ts,
    t.`_is_current` = FALSE,
    t.`_is_deleted` = TRUE
WHERE t.`_is_current`
  AND NOT EXISTS (
    SELECT 1 FROM {staging} s WHERE {join_ts}
  );

-- 3. Open a version for every key that no longer has a matching current row:
--    brand new keys, changed keys closed by (1), and keys resurrected after
--    having been soft-deleted by (2) in an earlier run.
INSERT INTO {target} ({insert_cols})
SELECT {select_cols},
       @run_ts,
       TIMESTAMP '{far_future}',
       TRUE,
       FALSE,
       @load_id,
       @source_tab
FROM {staging} s
WHERE NOT EXISTS (
  SELECT 1 FROM {target} t
  WHERE t.`_is_current` AND {join_ts} AND t.`_row_hash` = s.`_row_hash`
);

COMMIT TRANSACTION;""".format(
        target=target_ref,
        staging=staging_ref,
        join_ts=_key_join(key_columns, "t", "s"),
        insert_cols=insert_cols,
        select_cols=select_cols,
        far_future=FAR_FUTURE,
    )


def snapshot_insert_script(
    target_ref: str, staging_ref: str, data_columns: Sequence[str]
) -> str:
    """Append-only load for tabs with no business key.

    No versioning is attempted. Every run appends a complete, timestamped
    snapshot, which is honest about what is known: without a key there is no
    way to tell an edited row from a deleted row plus a new one.
    """
    insert_cols = (
        _col_list(data_columns)
        + ", `_raw`, `_row_hash`, `_load_id`, `_ingested_at`, `_source_tab`"
    )
    select_cols = _col_list(data_columns, prefix="s.") + ", s.`_raw`, s.`_row_hash`"
    return (
        "INSERT INTO %s (%s)\n"
        "SELECT %s, @load_id, @run_ts, @source_tab\n"
        "FROM %s s" % (target_ref, insert_cols, select_cols, staging_ref)
    )


def duplicate_key_query(staging_ref: str, key_columns: Sequence[str]) -> str:
    """Find business keys appearing more than once in one load.

    A duplicate key breaks the SCD2 invariant of one current row per key, and
    no automatic resolution is safe: keeping the last row assumes sheet order
    means recency, which nothing guarantees. So this is reported and the tab
    is refused rather than silently half-loaded.
    """
    cols = _col_list(key_columns)
    return (
        "SELECT %s, COUNT(*) AS n\n"
        "FROM %s\n"
        "GROUP BY %s\n"
        "HAVING n > 1\n"
        "ORDER BY n DESC\n"
        "LIMIT 20" % (cols, staging_ref, cols)
    )
