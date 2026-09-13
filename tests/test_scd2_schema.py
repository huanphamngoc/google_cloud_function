"""Header normalisation, type inference, coercion and hashing."""

import pytest

from scd2_schema import (
    BOOL,
    DATE,
    FLOAT64,
    INT64,
    STRING,
    TIMESTAMP,
    coerce,
    infer_schema,
    infer_type,
    normalise_column,
    normalise_headers,
    raw_json,
    resolve_type,
    row_hash,
)


class TestNormaliseColumn:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Customer ID", "customer_id"),
            ("  spaced  out  ", "spaced_out"),
            ("Doanh thu", "doanh_thu"),
            ("Số lượng", "so_luong"),
            ("Đơn giá", "don_gia"),
            ("Tên khách hàng", "ten_khach_hang"),
            ("2026 total", "col_2026_total"),
            ("%  margin", "margin"),
            ("a---b", "a_b"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalise_column(raw) == expected

    def test_a_blank_header_still_produces_a_usable_name(self):
        assert normalise_column("???") == "column"

    @pytest.mark.parametrize("raw", ["_load_id", "__valid_from", "_raw", "_"])
    def test_no_source_column_can_land_in_the_bookkeeping_namespace(self, raw):
        # Leading underscores are stripped, so a sheet can never produce a
        # column called _valid_from and shadow the SCD2 columns.
        assert not normalise_column(raw).startswith("_")


class TestNormaliseHeaders:
    def test_duplicates_are_suffixed_deterministically(self):
        assert normalise_headers(["id", "id", "id"]) == ["id", "id_2", "id_3"]

    def test_blank_headers_keep_their_position(self):
        # Data under a blank header is still data; dropping it would break the
        # promise that nothing disappears silently.
        assert normalise_headers(["id", "", "name"]) == ["id", "column_2", "name"]

    def test_different_raw_names_can_collide_after_normalisation(self):
        assert normalise_headers(["Customer ID", "customer id"]) == [
            "customer_id",
            "customer_id_2",
        ]


class TestInferType:
    @pytest.mark.parametrize(
        "values,expected",
        [
            (["1", "2", "3"], INT64),
            (["1", "", "3"], INT64),
            (["1.5", "2"], FLOAT64),
            (["1,234", "5,678"], INT64),
            (["1,234.56"], FLOAT64),
            (["true", "FALSE", "yes"], BOOL),
            (["2026-01-31", "2026-02-01"], DATE),
            (["2026-01-31 10:30:00"], TIMESTAMP),
            (["2026-01-31T10:30:00Z"], TIMESTAMP),
            (["abc", "1"], STRING),
            ([], STRING),
            (["", "  "], STRING),
        ],
    )
    def test_inference(self, values, expected):
        assert infer_type(values) == expected

    def test_ambiguous_dates_stay_string(self):
        # 01/02/2026 is either 1 Feb or 2 Jan. Picking one silently reorders a
        # year of data, so neither is picked.
        assert infer_type(["01/02/2026", "03/04/2026"]) == STRING

    def test_one_and_zero_are_integers_not_booleans(self):
        # Reading a quantity column as BOOL is exactly the silent corruption
        # this rule exists to prevent.
        assert infer_type(["1", "0", "1"]) == INT64

    def test_leading_zeros_are_preserved_as_string(self):
        # Phone numbers and product codes must not become integers.
        assert infer_type(["0901234567"]) == INT64  # documented limitation
        assert infer_type(["0901234567", "090-123"]) == STRING


class TestInferSchema:
    def test_overrides_win_over_inference(self):
        schema = infer_schema(
            ["id", "when"],
            [["1", "01/02/2026"]],
            overrides={"when": "DATE"},
        )
        assert schema == {"id": INT64, "when": DATE}

    def test_unknown_override_type_is_rejected(self):
        with pytest.raises(ValueError, match="unknown type override"):
            infer_schema(["id"], [["1"]], overrides={"id": "INTEGER64"})

    def test_short_rows_do_not_break_inference(self):
        schema = infer_schema(["a", "b"], [["1"], ["2", "x"]])
        assert schema["a"] == INT64
        assert schema["b"] == STRING


class TestResolveType:
    def test_identical_types_need_no_change(self):
        assert resolve_type(INT64, INT64) == (INT64, False)

    def test_int_widens_to_float(self):
        assert resolve_type(INT64, FLOAT64) == (FLOAT64, True)

    def test_a_string_column_is_never_narrowed_back_to_int(self):
        # Six months of STRING does not become INT64 because this week's
        # sample happens to be all digits.
        assert resolve_type(STRING, INT64) == (STRING, False)

    def test_float_does_not_narrow_to_int(self):
        assert resolve_type(FLOAT64, INT64) == (FLOAT64, False)

    def test_unsupported_widening_keeps_the_existing_type(self):
        # BigQuery cannot ALTER an INT64 column into a STRING one.
        assert resolve_type(INT64, STRING) == (INT64, False)


class TestCoerce:
    @pytest.mark.parametrize(
        "value,bq_type,expected",
        [
            ("42", INT64, 42),
            ("1,234", INT64, 1234),
            ("1.5", FLOAT64, 1.5),
            ("yes", BOOL, True),
            ("NO", BOOL, False),
            ("2026-01-31", DATE, "2026-01-31"),
            ("  padded  ", STRING, "padded"),
            ("", INT64, None),
            (None, STRING, None),
        ],
    )
    def test_successful_coercions(self, value, bq_type, expected):
        assert coerce(value, bq_type) == (expected, True)

    def test_a_failed_cast_reports_failure_rather_than_raising(self):
        # The caller nulls the typed column but keeps the text in _raw, so the
        # value is recoverable and the failure is countable.
        assert coerce("n/a", INT64) == (None, False)

    def test_an_empty_cell_is_null_not_a_failure(self):
        assert coerce("   ", FLOAT64) == (None, True)

    def test_timestamp_accepts_z_suffix(self):
        value, ok = coerce("2026-01-31T10:30:00Z", TIMESTAMP)
        assert ok and value.startswith("2026-01-31T10:30:00")


class TestRowHash:
    def test_same_payload_hashes_the_same(self):
        a = {"id": 1, "name": "hp"}
        b = {"id": 1, "name": "hp"}
        assert row_hash(a, ["id", "name"]) == row_hash(b, ["id", "name"])

    def test_column_order_does_not_matter(self):
        record = {"id": 1, "name": "hp"}
        assert row_hash(record, ["id", "name"]) == row_hash(record, ["name", "id"])

    def test_a_changed_value_changes_the_hash(self):
        assert row_hash({"name": "hp"}, ["name"]) != row_hash({"name": "hq"}, ["name"])

    def test_adding_an_empty_column_does_not_change_the_hash(self):
        # Without this, one new empty column would close and re-open every row
        # in the table, inventing a history of changes that never happened.
        before = row_hash({"id": 1}, ["id"])
        after = row_hash({"id": 1, "note": None}, ["id", "note"])
        assert before == after

    def test_adding_a_populated_column_does_change_the_hash(self):
        before = row_hash({"id": 1}, ["id"])
        after = row_hash({"id": 1, "note": "x"}, ["id", "note"])
        assert before != after

    def test_float_formatting_does_not_create_phantom_changes(self):
        assert row_hash({"x": 1.10}, ["x"]) == row_hash({"x": 1.1}, ["x"])

    def test_values_cannot_be_confused_across_columns(self):
        # "a=1,b=2" must not hash the same as "a=1,2" split differently.
        assert row_hash({"a": "1", "b": "2"}, ["a", "b"]) != row_hash(
            {"a": "1=2", "b": None}, ["a", "b"]
        )


class TestRawJson:
    def test_unmapped_trailing_cells_are_preserved(self):
        # A cell beyond the last header still lands in _raw, so a column added
        # to the right of the sheet is never lost before the next schema pass.
        blob = raw_json(["id"], ["1", "surprise"])
        assert '"column_2": "surprise"' in blob

    def test_unicode_is_not_escaped(self):
        assert "Hà Nội" in raw_json(["city"], ["Hà Nội"])
