"""Generated BigQuery SQL. Asserted as text so it can be reviewed in a diff."""

from __future__ import annotations

import pytest

from clearstate_ingest import (
    DType,
    Field,
    Schema,
    dataform_sqlx,
    landing_table_ddl,
    recon_sql,
    staging_view_sql,
)

LANDING = "proj.landing.raw_market_inputs"
STAGING = "proj.staging.stg_market_inputs"


class TestLandingDDL:
    def test_declares_every_contract_column(self, contract):
        ddl = landing_table_ddl(contract, LANDING)
        for f in contract.schema:
            assert "{} {}".format(f.name, f.dtype.value) in ddl

    def test_carries_the_lineage_columns(self, contract):
        ddl = landing_table_ddl(contract, LANDING)
        for col in ("load_id", "_ingested_at", "_row_hash", "_source_hash", "_contract_version"):
            assert col in ddl
        assert "_raw JSON" in ddl

    def test_partitions_and_clusters(self, contract):
        ddl = landing_table_ddl(contract, LANDING)
        assert "PARTITION BY DATE(_ingested_at)" in ddl
        assert "CLUSTER BY country_code, segment_id, year" in ddl

    def test_requires_a_partition_filter(self, contract):
        # an accidental full scan should fail rather than bill
        assert "require_partition_filter = TRUE" in landing_table_ddl(contract, LANDING)


class TestStagingView:
    def test_deduplicates_to_the_latest_row_per_grain(self, contract):
        sql = staging_view_sql(contract, LANDING, STAGING)
        assert "PARTITION BY country_code, segment_id, year" in sql
        assert "ORDER BY _ingested_at DESC" in sql
        assert "WHERE _rn = 1" in sql

    def test_aliased_column_falls_back_to_raw(self, contract):
        """A rename that predates the alias is still recoverable from _raw."""
        sql = staging_view_sql(contract, LANDING, STAGING)
        assert "COALESCE(asp_usd" in sql
        assert "JSON_VALUE(_raw, '$.average_selling_price')" in sql

    def test_late_adopted_column_is_backfilled_from_raw(self, contract):
        evolved = contract.bumped(
            Schema(
                contract.schema.fields
                + (Field("confidence_code", DType.STRING, since_version=2),)
            )
        )
        sql = staging_view_sql(evolved, LANDING, STAGING)
        assert (
            "COALESCE(confidence_code, "
            "SAFE_CAST(JSON_VALUE(_raw, '$.confidence_code') AS STRING)) AS confidence_code"
        ) in sql

    def test_plain_columns_are_not_wrapped(self, contract):
        sql = staging_view_sql(contract, LANDING, STAGING)
        assert "\n  country_code,\n" in sql
        assert "COALESCE(country_code" not in sql

    def test_casts_use_the_declared_type(self, contract):
        evolved = contract.bumped(
            Schema(contract.schema.fields + (Field("score", DType.FLOAT64, since_version=2),))
        )
        assert "AS FLOAT64)) AS score" in staging_view_sql(evolved, LANDING, STAGING)


class TestReconSQL:
    def test_full_outer_join_on_the_grain(self, contract):
        sql = recon_sql(contract, "proj.recon.baseline_x", "proj.recon.candidate_x")
        assert "FULL OUTER JOIN" in sql
        assert "USING (country_code, segment_id, year)" in sql

    def test_all_four_statuses_are_classified(self, contract):
        sql = recon_sql(contract, "b", "c")
        for status in ("extra_in_new", "missing_in_new", "match", "variance"):
            assert "'{}'".format(status) in sql

    def test_tolerances_come_from_the_contract(self, contract):
        sql = recon_sql(contract, "b", "c")
        assert "GREATEST(0.01, ABS(baseline_value) * 0.001)" in sql

    def test_measure_can_be_overridden(self, contract):
        sql = recon_sql(contract, "b", "c", measure="volume_units")
        assert "b.volume_units AS baseline_value" in sql

    def test_contract_without_measures_raises(self, tiny_contract):
        with pytest.raises(ValueError, match="no recon measures"):
            recon_sql(tiny_contract, "b", "c")


class TestDataformModel:
    def test_assertions_are_generated_from_the_contract(self, contract):
        sqlx = dataform_sqlx(contract, STAGING)
        assert '"uniqueKey"' in sqlx
        assert '"country_code"' in sqlx
        assert '"nonNull"' in sqlx

    def test_range_checks_become_row_conditions(self, contract):
        sqlx = dataform_sqlx(contract, STAGING)
        assert "rowConditions" in sqlx
        assert "NOT (year < 2015)" in sqlx
        assert "NOT (volume_units < 0)" in sqlx

    def test_description_records_owner_and_version(self, contract):
        sqlx = dataform_sqlx(contract, STAGING)
        assert "market_inputs v1" in sqlx
        assert contract.owner in sqlx
