"""The widening lattice and type inference — everything else reasons over these."""

from __future__ import annotations

import datetime as dt

import pytest

from clearstate_ingest import DType, Field, Schema, coerce, infer_dtype, infer_schema, normalise
from clearstate_ingest.schema import common_supertype, is_narrowing, is_widening


class TestWideningLattice:
    @pytest.mark.parametrize(
        "src,dst",
        [
            (DType.INT64, DType.FLOAT64),
            (DType.INT64, DType.NUMERIC),
            (DType.INT64, DType.STRING),
            (DType.BOOL, DType.INT64),
            (DType.BOOL, DType.STRING),      # transitive
            (DType.DATE, DType.TIMESTAMP),   # transitive
            (DType.FLOAT64, DType.STRING),
        ],
    )
    def test_widening_pairs(self, src, dst):
        assert is_widening(src, dst)
        assert is_narrowing(dst, src)

    @pytest.mark.parametrize(
        "src,dst",
        [
            (DType.FLOAT64, DType.INT64),
            (DType.STRING, DType.INT64),
            (DType.TIMESTAMP, DType.DATE),
        ],
    )
    def test_narrowing_pairs(self, src, dst):
        assert is_narrowing(src, dst)
        assert not is_widening(src, dst)

    def test_no_type_widens_to_itself(self):
        for t in DType:
            assert not is_widening(t, t)
            assert not is_narrowing(t, t)

    def test_dates_and_numbers_are_incompatible(self):
        # neither direction is safe: they meet only at STRING
        assert not is_widening(DType.DATE, DType.INT64)
        assert not is_narrowing(DType.DATE, DType.INT64)
        assert common_supertype(DType.DATE, DType.INT64) == DType.STRING

    def test_string_is_the_top_of_the_lattice(self):
        for t in DType:
            if t != DType.STRING:
                assert is_widening(t, DType.STRING)

    def test_common_supertype_prefers_the_narrowest(self):
        assert common_supertype(DType.INT64, DType.FLOAT64) == DType.FLOAT64
        assert common_supertype(DType.INT64, DType.INT64) == DType.INT64
        assert common_supertype(DType.DATE, DType.DATETIME) == DType.DATETIME


class TestInference:
    @pytest.mark.parametrize(
        "values,expected",
        [
            (["1", "2", "3"], DType.INT64),
            (["1", "2.5"], DType.FLOAT64),
            (["1.5", "2.5"], DType.FLOAT64),
            (["true", "false"], DType.BOOL),
            (["2026-01-05", "2026-02-02"], DType.DATE),
            (["SG", "MY"], DType.STRING),
            (["1", "SG"], DType.STRING),          # mixed collapses to STRING
            (["1,234", "5,678"], DType.INT64),    # thousands separators
        ],
    )
    def test_infer_dtype(self, values, expected):
        assert infer_dtype(values) == expected

    def test_nulls_are_ignored(self):
        assert infer_dtype(["", "n/a", "5", None, "#N/A"]) == DType.INT64

    def test_all_null_column_infers_string(self):
        # STRING is the safe default: a later real value registers as a narrowing
        # and gets surfaced rather than silently coerced
        assert infer_dtype(["", None, "n/a"]) == DType.STRING
        assert infer_dtype([]) == DType.STRING

    def test_infer_schema_from_grid(self):
        schema = infer_schema(
            ["Country Code", "Volume Units", "ASP USD"],
            [["SG", "100", "18.40"], ["MY", "200", "32.75"]],
        )
        assert schema.names == ("country_code", "volume_units", "asp_usd")
        assert [f.dtype for f in schema] == [DType.STRING, DType.INT64, DType.FLOAT64]


class TestNormalise:
    @pytest.mark.parametrize(
        "raw",
        ["country_code", "Country Code", "COUNTRY  CODE ", "country-code", " Country_Code "],
    )
    def test_header_variants_are_the_same_name(self, raw):
        assert normalise(raw) == "country_code"

    def test_leading_and_trailing_separators_are_stripped(self):
        assert normalise("  __value (USD)__ ") == "value_usd"


class TestCoerce:
    def test_casts_to_declared_type(self):
        assert coerce("1,234", DType.INT64) == 1234
        assert coerce("18.40", DType.FLOAT64) == 18.40
        assert coerce("yes", DType.BOOL) is True
        assert coerce("2026-01-05", DType.DATE) == dt.date(2026, 1, 5)

    def test_nullish_becomes_none(self):
        for value in ["", "  ", "n/a", "#N/A", None]:
            assert coerce(value, DType.INT64) is None

    def test_impossible_cast_raises(self):
        # this is what turns a declared-vs-actual mismatch into a quarantined row
        with pytest.raises(ValueError):
            coerce("41200.5", DType.INT64)
        with pytest.raises(ValueError):
            coerce("SG", DType.FLOAT64)


class TestFingerprint:
    def _schema(self, names):
        return Schema(tuple(Field(name=n, dtype=DType.STRING) for n in names))

    def test_reordering_does_not_change_the_fingerprint(self):
        # the whole system maps by name, so column order is not a schema change
        assert self._schema(["a", "b", "c"]).fingerprint() == self._schema(
            ["c", "a", "b"]
        ).fingerprint()

    def test_adding_a_column_changes_the_fingerprint(self):
        assert self._schema(["a", "b"]).fingerprint() != self._schema(["a", "b", "c"]).fingerprint()

    def test_changing_a_type_changes_the_fingerprint(self):
        a = Schema((Field("x", DType.INT64),))
        b = Schema((Field("x", DType.FLOAT64),))
        assert a.fingerprint() != b.fingerprint()


class TestFieldLookup:
    def test_get_resolves_aliases_and_casing(self):
        schema = Schema((Field("asp_usd", DType.FLOAT64, aliases=("average_selling_price",)),))
        assert schema.get("asp_usd").name == "asp_usd"
        assert schema.get("ASP USD").name == "asp_usd"
        assert schema.get("average_selling_price").name == "asp_usd"
        assert schema.get("unrelated") is None

    def test_with_alias_is_idempotent(self):
        f = Field("asp_usd", DType.FLOAT64)
        once = f.with_alias("Average Selling Price")
        twice = once.with_alias("average_selling_price")
        assert len(twice.aliases) == 1
