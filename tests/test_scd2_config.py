"""Configuration parsing and validation.

The value of a config-driven pipeline disappears if a bad config is accepted
quietly, so most of these tests are about what gets rejected.
"""

import pytest

from scd2_config import MODE_SCD2, MODE_SNAPSHOT, ConfigError, parse_config


def base(**overrides):
    data = {"spreadsheet_id": "abc123", "tabs": {}}
    data.update(overrides)
    return data


class TestRequiredFields:
    def test_spreadsheet_id_is_required(self):
        with pytest.raises(ConfigError, match="spreadsheet_id"):
            parse_config({"tabs": {}})

    def test_root_must_be_a_mapping(self):
        with pytest.raises(ConfigError, match="mapping"):
            parse_config([])


class TestBusinessKey:
    def test_scd2_without_a_key_is_rejected(self):
        # Versioning without a key cannot tell an edit from a delete plus an
        # insert, so this must fail loudly at config time.
        with pytest.raises(ConfigError, match="requires `business_key`"):
            parse_config(base(tabs={"Orders": {"mode": "scd2"}}))

    def test_snapshot_without_a_key_is_fine(self):
        config = parse_config(base(tabs={"Notes": {"mode": "snapshot"}}))
        assert config.tabs["Notes"].mode == MODE_SNAPSHOT

    def test_a_scalar_key_is_accepted_as_a_single_column(self):
        config = parse_config(base(tabs={"Orders": {"business_key": "order_id"}}))
        assert config.tabs["Orders"].business_key == ("order_id",)

    def test_key_columns_are_normalised_like_headers(self):
        # The key in the config must match the column name the loader derives
        # from the header, or the join would silently match nothing.
        config = parse_config(base(tabs={"Orders": {"business_key": ["Order ID"]}}))
        assert config.tabs["Orders"].business_key == ("order_id",)

    def test_composite_keys_keep_their_order(self):
        config = parse_config(
            base(tabs={"FX": {"business_key": ["currency", "rate_date"]}})
        )
        assert config.tabs["FX"].business_key == ("currency", "rate_date")


class TestValidation:
    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ConfigError, match="mode must be one of"):
            parse_config(base(tabs={"A": {"mode": "upsert", "business_key": ["id"]}}))

    def test_unknown_type_override_is_rejected(self):
        with pytest.raises(ConfigError, match="unknown BigQuery type"):
            parse_config(
                base(tabs={"A": {"business_key": ["id"], "types": {"id": "INTEGER"}}})
            )

    def test_two_tabs_cannot_target_the_same_table(self):
        # Otherwise two unrelated sheets would version into one another.
        with pytest.raises(ConfigError, match="same table name"):
            parse_config(
                base(
                    tabs={
                        "Jan": {"table": "sales", "business_key": ["id"]},
                        "Feb": {"table": "sales", "business_key": ["id"]},
                    }
                )
            )

    def test_a_key_column_cannot_also_be_excluded(self):
        with pytest.raises(ConfigError, match="both a business key and excluded"):
            parse_config(
                base(
                    tabs={
                        "A": {
                            "business_key": ["id"],
                            "exclude_columns": ["id"],
                        }
                    }
                )
            )

    def test_header_row_must_be_positive(self):
        with pytest.raises(ConfigError, match="header_row"):
            parse_config(base(tabs={"A": {"business_key": ["id"], "header_row": 0}}))

    def test_unknown_duplicate_policy_is_rejected(self):
        with pytest.raises(ConfigError, match="on_duplicate_key"):
            parse_config(
                base(tabs={"A": {"business_key": ["id"], "on_duplicate_key": "ignore"}})
            )


class TestTabResolution:
    def test_a_declared_tab_keeps_its_settings(self):
        config = parse_config(base(tabs={"Sheet1": {"business_key": ["id"]}}))
        resolved = config.tab_config("Sheet1")
        assert resolved.mode == MODE_SCD2
        assert resolved.configured is True

    def test_an_undeclared_tab_falls_back_to_snapshot(self):
        # A discovered tab has no business key. Guessing one would quietly
        # produce a wrong version history, so it is loaded append-only instead.
        config = parse_config(base())
        resolved = config.tab_config("Some New Tab")
        assert resolved.mode == MODE_SNAPSHOT
        assert resolved.configured is False
        assert resolved.table == "some_new_tab"

    def test_table_prefix_applies_to_declared_and_discovered_tabs(self):
        config = parse_config(
            base(table_prefix="hr_", tabs={"Staff": {"business_key": ["id"]}})
        )
        assert config.tabs["Staff"].table == "hr_staff"
        assert config.tab_config("Other").table == "hr_other"


class TestExclusions:
    def test_patterns_are_matched_as_regexes(self):
        config = parse_config(base(exclude_tabs=["^_", "(?i)readme"]))
        assert config.is_excluded("_scratch")
        assert config.is_excluded("README")
        assert config.is_excluded("Readme notes")
        assert not config.is_excluded("Orders")

    def test_a_declared_tab_beats_an_exclusion_pattern(self):
        # Explicit configuration is a stronger signal than a blanket pattern.
        config = parse_config(
            base(exclude_tabs=["(?i)note"], tabs={"Notes": {"mode": "snapshot"}})
        )
        assert not config.is_excluded("Notes")

    def test_a_malformed_pattern_does_not_take_the_run_down(self):
        config = parse_config(base(exclude_tabs=["[unclosed"]))
        assert config.is_excluded("[unclosed") is True
        assert config.is_excluded("Orders") is False


class TestDefaults:
    def test_sensible_dataset_defaults(self):
        config = parse_config(base())
        assert (config.dataset, config.staging_dataset, config.ops_dataset) == (
            "core",
            "staging",
            "ops",
        )
        assert config.discover_tabs is True
        assert config.create_views is True
