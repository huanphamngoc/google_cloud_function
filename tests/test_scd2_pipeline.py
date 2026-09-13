"""Orchestration: what process_tab decides to do, given what it reads.

BigQuery and Sheets are replaced with fakes that record the calls, so these
tests cover the decisions - block vs load, add column vs keep type, SCD2 vs
snapshot - without a cloud account.

What they deliberately do NOT cover: the merge SQL is recorded, not executed.
Its semantics are asserted structurally in test_scd2_sql.py. Confirming that
the three statements produce the right rows in BigQuery needs a real dataset,
and is listed as an open item in the package README.
"""

import datetime as dt

import pytest

import main
import scd2_ops as ops
from scd2_config import parse_config

RUN_TS = dt.datetime(2026, 9, 13, 6, 0, 0)
LOAD_ID = "test-load-id"


class FakeBigQuery(object):
    """Records every statement instead of running it."""

    def __init__(self, existing_schema=None, counts=None):
        self.project = "proj"
        self._existing = dict(existing_schema) if existing_schema else None
        self._counts = counts or {"inserted": 0, "closed": 0, "deleted": 0}
        self.statements = []
        self.staged = []
        self.datasets = []
        self.inserted_rows = []

    def ensure_dataset(self, dataset, default_table_expiration_ms=None):
        self.datasets.append(dataset)

    def table_schema(self, dataset, table):
        return dict(self._existing) if self._existing is not None else None

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return []

    def query_rows(self, sql, params=None):
        self.statements.append(sql)
        return [dict(self._counts)]

    def load_staging(self, dataset, table, records, schema):
        self.staged.append({"table": table, "records": list(records), "schema": dict(schema)})
        return len(records)

    def insert_rows(self, dataset, table, rows):
        self.inserted_rows.extend(rows)
        return []

    # -- helpers for assertions -------------------------------------------

    def sql_containing(self, needle):
        return [s for s in self.statements if needle in s]

    def ran(self, needle):
        return bool(self.sql_containing(needle))


@pytest.fixture
def config():
    return parse_config(
        {
            "spreadsheet_id": "sheet-abc",
            "tabs": {"Sheet1": {"table": "customer", "business_key": ["id"]}},
        }
    )


@pytest.fixture
def tab(config):
    return config.tabs["Sheet1"]


def run(bq, config, tab_config, header, rows, monkeypatch, dry_run=False):
    monkeypatch.setattr(main.sheets, "read_tab", lambda *a, **k: (header, rows))
    return main.process_tab(bq, object(), config, tab_config, LOAD_ID, RUN_TS, dry_run)


# ---------------------------------------------------------------------------


class TestFirstLoad:
    def test_a_new_table_is_created_partitioned_and_clustered(
        self, config, tab, monkeypatch
    ):
        bq = FakeBigQuery(existing_schema=None, counts={"inserted": 2, "closed": 0, "deleted": 0})
        result = run(bq, config, tab, ["id", "name"], [["1", "hp"], ["2", "an"]], monkeypatch)

        assert result.status == ops.STATUS_OK
        create = bq.sql_containing("CREATE TABLE IF NOT EXISTS")
        assert create and "PARTITION BY DATE(_valid_from)" in create[0]

    def test_rows_are_staged_with_the_inferred_schema(self, config, tab, monkeypatch):
        bq = FakeBigQuery(counts={"inserted": 1, "closed": 0, "deleted": 0})
        run(bq, config, tab, ["id", "name"], [["1", "hp"]], monkeypatch)

        assert len(bq.staged) == 1
        staged = bq.staged[0]
        assert staged["table"] == "customer__stg"
        assert staged["schema"] == {"id": "INT64", "name": "STRING"}
        assert staged["records"][0]["id"] == 1

    def test_the_merge_runs_after_staging(self, config, tab, monkeypatch):
        bq = FakeBigQuery(counts={"inserted": 1, "closed": 0, "deleted": 0})
        run(bq, config, tab, ["id"], [["1"]], monkeypatch)
        assert bq.ran("BEGIN TRANSACTION")

    def test_both_views_are_created(self, config, tab, monkeypatch):
        bq = FakeBigQuery(counts={"inserted": 1, "closed": 0, "deleted": 0})
        run(bq, config, tab, ["id"], [["1"]], monkeypatch)
        assert bq.ran("`proj.core.v_customer`")
        assert bq.ran("`proj.core.v_customer_history`")

    def test_counts_come_from_the_table_not_from_the_staged_row_count(
        self, config, tab, monkeypatch
    ):
        # The ops log should describe what is in the table, not what was
        # intended - an unchanged row stages but does not insert.
        bq = FakeBigQuery(counts={"inserted": 1, "closed": 1, "deleted": 3})
        result = run(bq, config, tab, ["id"], [["1"], ["2"]], monkeypatch)
        assert (result.rows_staged, result.rows_inserted) == (2, 1)
        assert (result.rows_closed, result.rows_deleted) == (1, 3)


class TestSchemaEvolution:
    def test_a_new_column_is_added_to_the_existing_table(self, config, tab, monkeypatch):
        bq = FakeBigQuery(existing_schema={"id": "INT64", "_row_hash": "STRING"})
        result = run(bq, config, tab, ["id", "email"], [["1", "a@b.c"]], monkeypatch)

        assert bq.ran("ADD COLUMN IF NOT EXISTS `email`")
        assert {"change_type": "column_added", "column_name": "email",
                "from_type": None, "to_type": "STRING"} in result.schema_changes

    def test_a_widenable_type_is_altered_in_place(self, config, tab, monkeypatch):
        bq = FakeBigQuery(existing_schema={"id": "INT64", "amount": "INT64"})
        result = run(bq, config, tab, ["id", "amount"], [["1", "1.5"]], monkeypatch)

        assert bq.ran("SET DATA TYPE FLOAT64")
        assert any(c["change_type"] == "type_widened" for c in result.schema_changes)

    def test_a_non_widenable_conflict_keeps_the_existing_column(
        self, config, tab, monkeypatch
    ):
        # The table has been STRING for months; this week's all-digit sample
        # must not narrow it.
        bq = FakeBigQuery(existing_schema={"id": "INT64", "code": "STRING"})
        result = run(bq, config, tab, ["id", "code"], [["1", "42"]], monkeypatch)

        assert not bq.ran("SET DATA TYPE")
        assert bq.staged[0]["schema"]["code"] == "STRING"
        assert any(c["change_type"] == "type_conflict_kept" for c in result.schema_changes)

    def test_a_column_that_vanished_from_the_sheet_is_recorded_not_dropped(
        self, config, tab, monkeypatch
    ):
        # Dropping it would destroy history that is still valid for older
        # versions, so it is only reported.
        bq = FakeBigQuery(existing_schema={"id": "INT64", "legacy": "STRING"})
        result = run(bq, config, tab, ["id"], [["1"]], monkeypatch)

        assert not bq.ran("DROP COLUMN")
        assert any(
            c["change_type"] == "column_missing_from_source" and c["column_name"] == "legacy"
            for c in result.schema_changes
        )

    def test_bookkeeping_columns_are_not_mistaken_for_source_columns(
        self, config, tab, monkeypatch
    ):
        bq = FakeBigQuery(
            existing_schema={"id": "INT64", "_valid_from": "TIMESTAMP", "_is_current": "BOOL"}
        )
        result = run(bq, config, tab, ["id"], [["1"]], monkeypatch)
        changed = [c["column_name"] for c in result.schema_changes]
        assert "_valid_from" not in changed and "_is_current" not in changed


class TestRefusals:
    def test_duplicate_keys_block_the_tab_and_write_nothing(
        self, config, tab, monkeypatch
    ):
        bq = FakeBigQuery()
        result = run(bq, config, tab, ["id", "name"], [["1", "a"], ["1", "b"]], monkeypatch)

        assert result.status == ops.STATUS_BLOCKED
        assert "duplicate business key" in result.message
        assert bq.staged == []
        assert not bq.ran("BEGIN TRANSACTION")

    def test_the_duplicate_message_names_the_sheet_rows_to_fix(
        self, config, tab, monkeypatch
    ):
        bq = FakeBigQuery()
        result = run(bq, config, tab, ["id"], [["1"], ["1"]], monkeypatch)
        assert "[2, 3]" in result.message

    def test_keep_last_collapses_duplicates_when_asked(self, config, monkeypatch):
        config = parse_config(
            {
                "spreadsheet_id": "s",
                "tabs": {
                    "Sheet1": {
                        "table": "customer",
                        "business_key": ["id"],
                        "on_duplicate_key": "keep_last",
                    }
                },
            }
        )
        bq = FakeBigQuery(counts={"inserted": 1, "closed": 0, "deleted": 0})
        result = run(
            bq, config, config.tabs["Sheet1"], ["id", "name"],
            [["1", "first"], ["1", "last"]], monkeypatch,
        )
        assert result.status == ops.STATUS_OK
        assert len(bq.staged[0]["records"]) == 1
        assert bq.staged[0]["records"][0]["name"] == "last"

    def test_an_empty_business_key_blocks_the_tab(self, config, tab, monkeypatch):
        bq = FakeBigQuery()
        result = run(bq, config, tab, ["id", "name"], [["", "hp"]], monkeypatch)
        assert result.status == ops.STATUS_BLOCKED
        assert "empty business key" in result.message

    def test_a_tab_emptied_of_data_is_refused_rather_than_mass_soft_deleted(
        self, config, tab, monkeypatch
    ):
        # "Everything was deleted" and "somebody cleared the tab by accident"
        # look identical here. Refusing is the reversible choice.
        bq = FakeBigQuery()
        result = run(bq, config, tab, ["id", "name"], [], monkeypatch)

        assert result.status == ops.STATUS_BLOCKED
        assert "soft-delete" in result.message
        assert not bq.ran("BEGIN TRANSACTION")

    def test_a_tab_with_no_header_is_skipped_not_failed(self, config, tab, monkeypatch):
        # A new month's sheet created but not filled in yet is normal.
        bq = FakeBigQuery()
        result = run(bq, config, tab, [], [], monkeypatch)
        assert result.status == ops.STATUS_SKIPPED

    def test_one_tab_failing_does_not_raise(self, config, tab, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("Sheets API 403")

        monkeypatch.setattr(main.sheets, "read_tab", boom)
        result = main.process_tab(
            FakeBigQuery(), object(), config, tab, LOAD_ID, RUN_TS
        )
        assert result.status == ops.STATUS_ERROR
        assert "403" in result.message


class TestSnapshotMode:
    def test_a_discovered_tab_loads_append_only_without_a_key(self, config, monkeypatch):
        discovered = config.tab_config("Ghi chu")
        bq = FakeBigQuery()
        result = run(bq, config, discovered, ["note"], [["xin chao"]], monkeypatch)

        assert result.status == ops.STATUS_OK
        assert bq.ran("INSERT INTO")
        assert not bq.ran("BEGIN TRANSACTION")

    def test_snapshot_tables_get_no_current_view(self, config, monkeypatch):
        # There is no "current" row without a key to define one.
        discovered = config.tab_config("Ghi chu")
        bq = FakeBigQuery()
        run(bq, config, discovered, ["note"], [["x"]], monkeypatch)
        assert not bq.ran("CREATE OR REPLACE VIEW")

    def test_an_empty_snapshot_tab_is_skipped_not_blocked(self, config, monkeypatch):
        discovered = config.tab_config("Ghi chu")
        bq = FakeBigQuery()
        result = run(bq, config, discovered, ["note"], [], monkeypatch)
        assert result.status == ops.STATUS_SKIPPED


class TestDryRun:
    def test_dry_run_reads_and_validates_but_writes_nothing(
        self, config, tab, monkeypatch
    ):
        bq = FakeBigQuery()
        result = run(bq, config, tab, ["id"], [["1"]], monkeypatch, dry_run=True)

        assert result.status == ops.STATUS_SKIPPED
        assert "dry run" in result.message
        assert bq.staged == [] and bq.statements == []

    def test_dry_run_still_catches_a_duplicate_key(self, config, tab, monkeypatch):
        bq = FakeBigQuery()
        result = run(bq, config, tab, ["id"], [["1"], ["1"]], monkeypatch, dry_run=True)
        assert result.status == ops.STATUS_BLOCKED


class TestTabSelection:
    def test_declared_and_discovered_tabs_are_both_returned(self, config, monkeypatch):
        monkeypatch.setattr(main.sheets, "list_tabs", lambda *a: ["Sheet1", "Extra"])
        selected = main.resolve_tabs(config, object())
        assert [t.tab for t in selected] == ["Sheet1", "Extra"]
        assert selected[0].mode == "scd2" and selected[1].mode == "snapshot"

    def test_excluded_tabs_are_left_out(self, monkeypatch):
        config = parse_config({"spreadsheet_id": "s", "exclude_tabs": ["^_"]})
        monkeypatch.setattr(main.sheets, "list_tabs", lambda *a: ["Data", "_scratch"])
        assert [t.tab for t in main.resolve_tabs(config, object())] == ["Data"]

    def test_an_explicit_tab_list_overrides_discovery(self, config, monkeypatch):
        monkeypatch.setattr(main.sheets, "list_tabs", lambda *a: ["Sheet1", "Extra"])
        selected = main.resolve_tabs(config, object(), only=["Extra"])
        assert [t.tab for t in selected] == ["Extra"]

    def test_a_declared_tab_missing_from_the_file_is_still_attempted(
        self, config, monkeypatch
    ):
        # Better to report "tab not found" against the config than to silently
        # stop loading a table someone still depends on.
        monkeypatch.setattr(main.sheets, "list_tabs", lambda *a: [])
        assert [t.tab for t in main.resolve_tabs(config, object())] == ["Sheet1"]


class TestOverallStatus:
    def test_all_ok_is_ok(self):
        results = [ops.TabResult("a", "a", "scd2"), ops.TabResult("b", "b", "scd2")]
        assert ops.overall_status(results) == ops.STATUS_OK

    def test_a_skipped_tab_does_not_spoil_an_otherwise_clean_run(self):
        results = [ops.TabResult("a", "a", "scd2"), ops.TabResult("b", "b", "scd2").skip("empty")]
        assert ops.overall_status(results) == ops.STATUS_OK

    def test_one_blocked_tab_makes_the_run_partial(self):
        # A caller checking only for "ok" must notice that a tab was refused.
        results = [ops.TabResult("a", "a", "scd2"), ops.TabResult("b", "b", "scd2").block("dup")]
        assert ops.overall_status(results) == "partial"

    def test_everything_failing_is_an_error(self):
        results = [ops.TabResult("a", "a", "scd2").fail("boom")]
        assert ops.overall_status(results) == ops.STATUS_ERROR

    def test_http_code_reflects_the_status(self):
        assert main._http_code(ops.STATUS_OK) == 200
        assert main._http_code("partial") == 207
        assert main._http_code(ops.STATUS_ERROR) == 500
