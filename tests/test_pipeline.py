"""End-to-end: the four sample snapshots through a real (SQLite) warehouse.

This is the test that proves the design claim - that a source sheet can change
shape four times and the pipeline responds differently and correctly each time,
without anyone editing Python.
"""

from __future__ import annotations

import json

import pytest

from clearstate_ingest import Action, ChangeType, Pipeline, SQLiteWarehouse, adopt_alias
from clearstate_ingest.extract import read_csv


def load(path):
    return read_csv(path, header_row=3)


@pytest.fixture
def pipeline(warehouse):
    return Pipeline(warehouse)


class TestVersionOne:
    def test_baseline_shape_loads_cleanly(self, pipeline, contract, sample):
        result = pipeline.run(contract, data=load(sample["v1"]))
        assert result.status == "success"
        assert result.rows_in == 18
        assert result.rows_loaded == 18
        assert result.plan.is_noop
        assert result.evolved_contract is None

    def test_rows_are_typed_in_the_warehouse(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v1"]))
        rows = warehouse.query(
            "SELECT * FROM landing_raw_market_inputs WHERE country_code='SG' "
            "AND segment_id='IVD-CHEM' AND year=2025"
        )
        assert len(rows) == 1
        assert rows[0]["volume_units"] == 41200
        assert rows[0]["value_usd"] == pytest.approx(758080.0)

    def test_lineage_columns_are_populated(self, pipeline, contract, sample, warehouse):
        result = pipeline.run(contract, data=load(sample["v1"]))
        row = warehouse.query("SELECT * FROM landing_raw_market_inputs LIMIT 1")[0]
        assert row["load_id"] == result.load_id
        assert row["_contract_version"] == 1
        assert row["_row_hash"] and row["_source_hash"] and row["_ingested_at"]

    def test_rerunning_the_same_load_id_is_idempotent(self, pipeline, contract, sample, warehouse):
        for _ in range(3):
            pipeline.run(contract, data=load(sample["v1"]), load_id="fixed-load")
        count = warehouse.query("SELECT COUNT(*) AS n FROM landing_raw_market_inputs")[0]["n"]
        assert count == 18


class TestVersionTwoAutoEvolution:
    def test_new_column_evolves_the_contract_and_keeps_loading(self, pipeline, contract, sample):
        result = pipeline.run(contract, data=load(sample["v2"]))
        assert result.status == "success"
        assert result.rows_loaded == 18
        assert result.plan.action == Action.AUTO
        assert result.evolved_contract.version == contract.version + 1
        assert result.evolved_contract.schema.get("confidence_code") is not None

    def test_the_new_column_reaches_the_table(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v2"]))
        cols = warehouse.columns("landing_raw_market_inputs")
        assert "confidence_code" in cols

    def test_the_value_is_preserved_in_raw_even_on_the_first_sighting(
        self, pipeline, contract, sample, warehouse
    ):
        """The retroactive back-fill guarantee from design/schema-evolution.md."""
        pipeline.run(contract, data=load(sample["v2"]))
        row = warehouse.query(
            "SELECT _raw FROM landing_raw_market_inputs WHERE country_code='SG' "
            "AND segment_id='IVD-CHEM' AND year=2025"
        )[0]
        assert json.loads(row["_raw"])["confidence_code"] == "H"

    def test_reordering_does_not_trigger_a_reload_decision(self, pipeline, contract, sample):
        # v2 also moves confidence_code to the front; that must not matter
        result = pipeline.run(contract, data=load(sample["v2"]))
        assert result.plan.action == Action.AUTO


class TestVersionThreeQuarantine:
    def test_inferred_rename_quarantines_rather_than_guessing(self, pipeline, contract, sample):
        result = pipeline.run(contract, data=load(sample["v3"]))
        assert result.status == "quarantined"
        assert result.plan.action == Action.PROPOSE
        assert ChangeType.COLUMN_RENAMED_INFERRED in {
            c.change_type for c in result.plan.changes
        }

    def test_landing_table_is_untouched_by_a_quarantined_run(
        self, pipeline, contract, sample, warehouse
    ):
        pipeline.run(contract, data=load(sample["v1"]))
        before = warehouse.query("SELECT COUNT(*) AS n FROM landing_raw_market_inputs")[0]["n"]
        pipeline.run(contract, data=load(sample["v3"]))
        after = warehouse.query("SELECT COUNT(*) AS n FROM landing_raw_market_inputs")[0]["n"]
        assert before == after == 18

    def test_quarantined_rows_carry_the_reason(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v3"]))
        rows = warehouse.query("SELECT * FROM landing_quarantine_market_inputs LIMIT 1")
        assert rows and "rename" in rows[0]["_quarantine_reason"]

    def test_the_patch_names_the_exact_edit(self, pipeline, contract, sample):
        result = pipeline.run(contract, data=load(sample["v3"]))
        patch = result.plan.contract_patch()
        assert patch["add_aliases"]["asp_usd"] == ["average_selling_price_usd"]

    def test_adopting_the_alias_turns_the_next_run_green(self, pipeline, contract, sample):
        first = pipeline.run(contract, data=load(sample["v3"]))
        assert first.status == "quarantined"

        confirmed = adopt_alias(contract, "asp_usd", "average_selling_price_usd")
        second = pipeline.run(confirmed, data=load(sample["v3"]))

        assert second.status == "success"
        assert second.plan.action == Action.AUTO
        assert second.rows_loaded == 18


class TestVersionFourBlocked:
    def test_losing_a_grain_column_blocks(self, pipeline, contract, sample):
        result = pipeline.run(contract, data=load(sample["v4"]))
        assert result.status == "blocked"
        assert result.rows_loaded == 0

    def test_nothing_is_written_anywhere(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v1"]))
        pipeline.run(contract, data=load(sample["v4"]))
        landed = warehouse.query("SELECT COUNT(*) AS n FROM landing_raw_market_inputs")[0]["n"]
        assert landed == 18  # only v1's rows
        tables = warehouse.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'landing_quarantine%'"
        )
        for t in tables:
            n = warehouse.query("SELECT COUNT(*) AS n FROM {}".format(t["name"]))[0]["n"]
            assert n == 0

    def test_both_blocking_reasons_are_reported(self, pipeline, contract, sample):
        result = pipeline.run(contract, data=load(sample["v4"]))
        kinds = {c.change_type for c in result.plan.blocking()}
        assert ChangeType.COLUMN_REMOVED_REQUIRED in kinds
        assert ChangeType.GRAIN_COLUMN_MISSING in kinds


class TestOpsTables:
    def test_every_run_writes_a_run_log_row(self, pipeline, contract, sample, warehouse):
        for key in ("v1", "v2", "v3", "v4"):
            pipeline.run(contract, data=load(sample[key]))
        runs = warehouse.query("SELECT status FROM ops_run_log ORDER BY started_at")
        assert len(runs) == 4
        assert {r["status"] for r in runs} == {"success", "quarantined", "blocked"}

    def test_schema_registry_records_each_distinct_shape(
        self, pipeline, contract, sample, warehouse
    ):
        pipeline.run(contract, data=load(sample["v1"]))
        pipeline.run(contract, data=load(sample["v1"]))  # same shape again
        pipeline.run(contract, data=load(sample["v2"]))
        fps = warehouse.query("SELECT fingerprint FROM ops_schema_registry")
        assert len(fps) == 2  # v1 seen twice, registered once

    def test_evolution_log_captures_every_decision(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v3"]))
        rows = warehouse.query("SELECT decision, change_type FROM ops_schema_evolution_log")
        decisions = {r["decision"] for r in rows}
        assert "propose" in decisions and "auto" in decisions

    def test_blocked_runs_are_still_logged(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v4"]))
        rows = warehouse.query(
            "SELECT * FROM ops_schema_evolution_log WHERE decision = 'block'"
        )
        assert rows

    def test_dq_results_are_written(self, pipeline, contract, sample, warehouse):
        pipeline.run(contract, data=load(sample["v1"]))
        rows = warehouse.query("SELECT * FROM ops_dq_results")
        assert rows and rows[0]["rows_in"] == 18

    def test_recon_results_are_written(self, pipeline, contract, sample, warehouse):
        baseline = read_csv(sample["baseline"]).as_dicts()
        candidate = read_csv(sample["candidate"]).as_dicts()
        result = pipeline.reconcile_against(contract, baseline, candidate, load_id="L1")
        rows = warehouse.query("SELECT * FROM ops_recon_results")
        assert len(rows) == 1
        assert rows[0]["passed"] == 0
        assert json.loads(rows[0]["scorecard_json"])["comparisons"] == result.total


class TestFullSequence:
    def test_four_snapshots_in_order_behave_as_designed(self, pipeline, contract, sample):
        """The headline scenario, start to finish."""
        outcomes = []
        working = contract
        for key in ("v1", "v2", "v3", "v4"):
            result = pipeline.run(working, data=load(sample[key]))
            outcomes.append(result.status)
            if result.evolved_contract:
                working = result.evolved_contract

        assert outcomes == ["success", "success", "quarantined", "blocked"]
        # the contract evolved exactly once, for the additive change
        assert working.version == 2
