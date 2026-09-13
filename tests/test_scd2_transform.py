"""Turning sheet values into staging records."""

import json

from scd2_transform import build_records, find_duplicate_keys, is_blank_row, missing_key_rows

SCHEMA = {"id": "INT64", "name": "STRING"}


class TestBuildRecords:
    def test_values_are_typed_not_left_as_text(self):
        data = build_records(["id", "name"], [["1", "hp"]], SCHEMA)
        assert data.records[0]["id"] == 1
        assert data.records[0]["name"] == "hp"

    def test_every_row_carries_raw_hash_and_source_row(self):
        data = build_records(["id", "name"], [["1", "hp"]], SCHEMA)
        record = data.records[0]
        assert set(["_raw", "_row_hash", "_source_row"]).issubset(record)

    def test_source_row_matches_what_a_person_sees_in_the_sheet(self):
        # Row 1 is the header, so the first data row is row 2.
        data = build_records(["id"], [["1"], ["2"]], {"id": "INT64"})
        assert [r["_source_row"] for r in data.records] == [2, 3]

    def test_blank_rows_are_skipped_and_counted(self):
        data = build_records(["id"], [["1"], ["", ""], ["2"]], {"id": "INT64"})
        assert data.row_count == 2
        assert data.rows_skipped == 1

    def test_a_failed_cast_nulls_the_column_but_keeps_the_text_in_raw(self):
        data = build_records(["id", "name"], [["n/a", "hp"]], SCHEMA)
        record = data.records[0]
        assert record["id"] is None
        assert json.loads(record["_raw"])["id"] == "n/a"
        assert data.coercion_errors == {"id": 1}

    def test_short_rows_are_padded_rather_than_dropped(self):
        data = build_records(["id", "name"], [["1"]], SCHEMA)
        assert data.records[0]["name"] is None

    def test_excluded_columns_leave_the_typed_output_but_stay_in_raw(self):
        data = build_records(
            ["id", "edited_by"],
            [["1", "an"]],
            {"id": "INT64", "edited_by": "STRING"},
            exclude_columns=["edited_by"],
        )
        assert "edited_by" not in data.records[0]
        assert json.loads(data.records[0]["_raw"])["edited_by"] == "an"

    def test_an_excluded_column_cannot_trigger_a_new_version(self):
        # This is the whole point of exclude_columns: a volatile "last edited
        # by" stamp should not open a new version on every run.
        first = build_records(
            ["id", "edited_by"],
            [["1", "an"]],
            {"id": "INT64", "edited_by": "STRING"},
            exclude_columns=["edited_by"],
        )
        second = build_records(
            ["id", "edited_by"],
            [["1", "binh"]],
            {"id": "INT64", "edited_by": "STRING"},
            exclude_columns=["edited_by"],
        )
        assert first.records[0]["_row_hash"] == second.records[0]["_row_hash"]

    def test_a_real_edit_does_change_the_hash(self):
        first = build_records(["id", "name"], [["1", "hp"]], SCHEMA)
        second = build_records(["id", "name"], [["1", "huan"]], SCHEMA)
        assert first.records[0]["_row_hash"] != second.records[0]["_row_hash"]

    def test_reordering_columns_in_the_sheet_does_not_change_the_hash(self):
        # Someone dragging a column sideways must not re-version the table.
        first = build_records(["id", "name"], [["1", "hp"]], SCHEMA)
        second = build_records(["name", "id"], [["hp", "1"]], SCHEMA)
        assert first.records[0]["_row_hash"] == second.records[0]["_row_hash"]


class TestBlankRow:
    def test_whitespace_only_counts_as_blank(self):
        assert is_blank_row(["", "  ", None])

    def test_a_single_value_makes_a_row_non_blank(self):
        assert not is_blank_row(["", "0"])


class TestKeyValidation:
    def test_duplicate_keys_are_reported_with_their_sheet_rows(self):
        data = build_records(["id"], [["1"], ["2"], ["1"]], {"id": "INT64"})
        duplicates = find_duplicate_keys(data.records, ["id"])
        assert len(duplicates) == 1
        key, rows = duplicates[0]
        assert key == (1,)
        assert rows == [2, 4]

    def test_unique_keys_report_nothing(self):
        data = build_records(["id"], [["1"], ["2"]], {"id": "INT64"})
        assert find_duplicate_keys(data.records, ["id"]) == []

    def test_composite_keys_are_only_duplicates_when_both_match(self):
        data = build_records(
            ["cur", "d"],
            [["USD", "2026-01-01"], ["USD", "2026-01-02"]],
            {"cur": "STRING", "d": "STRING"},
        )
        assert find_duplicate_keys(data.records, ["cur", "d"]) == []

    def test_rows_with_an_empty_key_are_reported(self):
        # A NULL key cannot be matched next run, so the row would insert a new
        # version every single time.
        data = build_records(["id", "name"], [["", "hp"], ["2", "x"]], SCHEMA)
        assert missing_key_rows(data.records, ["id"]) == [2]

    def test_no_key_columns_means_nothing_to_validate(self):
        data = build_records(["id"], [["1"], ["1"]], {"id": "INT64"})
        assert find_duplicate_keys(data.records, []) == []
        assert missing_key_rows(data.records, []) == []
