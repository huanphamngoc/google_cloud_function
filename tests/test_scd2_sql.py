"""The generated SQL.

These tests assert the exact SQL that will reach BigQuery. They do not execute
it - nothing here runs a query - so they cover the shape and the ordering of
the statements, which is where the SCD2 logic actually lives.
"""

import pytest

import scd2_sql as sql


@pytest.fixture
def target():
    return sql.qualify("proj", "core", "customer")


@pytest.fixture
def staging():
    return sql.qualify("proj", "staging", "customer__stg")


class TestQualify:
    def test_reference_is_backticked(self):
        assert sql.qualify("p", "d", "t") == "`p.d.t`"


class TestCreateTable:
    def test_source_and_bookkeeping_columns_are_both_present(self, target):
        ddl = sql.create_scd2_table_ddl(target, {"id": "INT64", "name": "STRING"}, ["id"])
        assert "`id` INT64" in ddl
        assert "`name` STRING" in ddl
        for column, _type in sql.SCD2_META:
            assert "`%s`" % column in ddl

    def test_partitioned_and_clustered_at_creation(self, target):
        ddl = sql.create_scd2_table_ddl(target, {"id": "INT64"}, ["id"])
        assert "PARTITION BY DATE(_valid_from)" in ddl
        # _is_current and the first key column are the two predicates almost
        # every read applies.
        assert "CLUSTER BY `_is_current`, `id`" in ddl

    def test_creation_is_idempotent(self, target):
        assert "IF NOT EXISTS" in sql.create_scd2_table_ddl(target, {"id": "INT64"}, ["id"])

    def test_snapshot_table_has_no_validity_columns(self, target):
        ddl = sql.create_snapshot_table_ddl(target, {"id": "INT64"})
        assert "_valid_from" not in ddl
        assert "`_ingested_at` TIMESTAMP" in ddl


class TestViews:
    def test_current_view_filters_deleted_and_superseded_rows(self, target):
        view = sql.qualify("proj", "core", "v_customer")
        ddl = sql.create_current_view_ddl(view, target, ["id", "name"])
        assert "WHERE `_is_current` AND NOT `_is_deleted`" in ddl

    def test_current_view_hides_the_bookkeeping_columns(self, target):
        view = sql.qualify("proj", "core", "v_customer")
        ddl = sql.create_current_view_ddl(view, target, ["id", "name"])
        assert "_row_hash" not in ddl
        assert "_is_deleted" in ddl  # only in the WHERE clause
        assert "SELECT `id`, `name`, `_valid_from`, `_load_id`" in ddl


class TestScd2Merge:
    def test_a_key_is_required(self, target, staging):
        with pytest.raises(ValueError, match="at least one business key"):
            sql.scd2_merge_script(target, staging, [], ["id"])

    def test_statements_run_in_the_order_the_logic_depends_on(self, target, staging):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id", "name"])
        close_changed = script.index("payload changed")
        close_deleted = script.index("soft delete")
        insert = script.index("INSERT INTO")
        # Both UPDATEs must precede the INSERT: the INSERT relies on a changed
        # key no longer having a current row by the time it is evaluated.
        assert close_changed < insert
        assert close_deleted < insert

    def test_the_whole_merge_is_one_transaction(self, target, staging):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id"])
        assert script.startswith("BEGIN TRANSACTION;")
        assert script.rstrip().endswith("COMMIT TRANSACTION;")

    def test_changed_rows_are_detected_by_hash_not_by_column_comparison(
        self, target, staging
    ):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id", "name"])
        assert "s.`_row_hash` != t.`_row_hash`" in script

    def test_only_current_rows_are_ever_closed(self, target, staging):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id"])
        # Closing a historical row would rewrite a validity window that has
        # already been reported on.
        for statement in script.split(";"):
            if statement.strip().startswith("UPDATE"):
                assert "t.`_is_current`" in statement

    def test_composite_keys_join_on_every_column(self, target, staging):
        script = sql.scd2_merge_script(
            target, staging, ["currency", "rate_date"], ["currency", "rate_date", "rate"]
        )
        assert "t.`currency` = s.`currency` AND t.`rate_date` = s.`rate_date`" in script

    def test_new_versions_open_with_the_far_future_sentinel(self, target, staging):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id"])
        # A sentinel rather than NULL keeps point-in-time reads a plain
        # BETWEEN, with no OR-IS-NULL branch a reader can forget.
        assert "TIMESTAMP '%s'" % sql.FAR_FUTURE in script

    def test_unchanged_rows_are_excluded_by_the_insert_predicate(self, target, staging):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id"])
        assert "WHERE NOT EXISTS" in script
        assert "t.`_is_current` AND t.`id` = s.`id` AND t.`_row_hash` = s.`_row_hash`" in script

    def test_run_metadata_is_parameterised_not_interpolated(self, target, staging):
        script = sql.scd2_merge_script(target, staging, ["id"], ["id"])
        assert "@run_ts" in script and "@load_id" in script and "@source_tab" in script


class TestSnapshotInsert:
    def test_snapshot_is_pure_append(self, target, staging):
        script = sql.snapshot_insert_script(target, staging, ["id", "note"])
        assert script.startswith("INSERT INTO")
        assert "UPDATE" not in script
        assert "DELETE" not in script
        assert "TRUNCATE" not in script


class TestDuplicateKeyQuery:
    def test_groups_by_the_key_and_reports_counts(self, staging):
        query = sql.duplicate_key_query(staging, ["id"])
        assert "GROUP BY `id`" in query
        assert "HAVING n > 1" in query
