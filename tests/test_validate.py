"""Value-level validation and the _raw preservation guarantee."""

from __future__ import annotations

import json

import pytest

from clearstate_ingest import DType, Field, Schema, canonicalise, plan_evolution, validate
from clearstate_ingest.contracts import parse_contract


def obs(**cols):
    return Schema(tuple(Field(name=n, dtype=t) for n, t in cols.items()))


@pytest.fixture
def strict_contract():
    return parse_contract(
        {
            "source_id": "strict",
            "grain": ["code", "year"],
            "columns": [
                {"name": "code", "type": "STRING", "required": True, "regex": "^[A-Z]{2}$"},
                {"name": "year", "type": "INT64", "required": True, "min": 2015, "max": 2040},
                {"name": "amount", "type": "FLOAT64", "required": True, "min": 0},
                {"name": "band", "type": "STRING", "required": False, "allowed": ["H", "M", "L"]},
            ],
        }
    )


class TestCanonicalise:
    def test_source_names_are_mapped_to_canonical_names(self, tiny_contract):
        plan = plan_evolution(tiny_contract, obs(ID=DType.STRING, Qty=DType.INT64))
        rows = canonicalise([{"id": "a", "qty": "5"}], plan)
        assert rows[0]["id"] == "a"
        assert rows[0]["qty"] == "5"

    def test_unmapped_columns_are_preserved_in_raw(self, tiny_contract):
        """The guarantee: a column nobody has adopted yet is still landed."""
        plan = plan_evolution(
            tiny_contract, obs(id=DType.STRING, qty=DType.INT64, surprise=DType.STRING)
        )
        rows = canonicalise([{"id": "a", "qty": "5", "surprise": "kept"}], plan)
        assert "surprise" not in rows[0]                     # not a real column yet
        assert json.loads(rows[0]["_raw"])["surprise"] == "kept"   # but not lost

    def test_raw_holds_every_source_column_mapped_or_not(self, tiny_contract):
        plan = plan_evolution(tiny_contract, obs(id=DType.STRING, qty=DType.INT64))
        raw = json.loads(canonicalise([{"id": "a", "qty": "5"}], plan)[0]["_raw"])
        assert raw == {"id": "a", "qty": "5"}


class TestValidation:
    def _rows(self, *rows):
        return [dict(r, _raw="{}") for r in rows]

    def test_clean_rows_pass_and_are_typed(self, strict_contract):
        report = validate(
            strict_contract,
            self._rows({"code": "SG", "year": "2026", "amount": "1.5", "band": "H"}),
        )
        assert report.ok
        assert report.rows_ok == 1
        row = report.clean_rows[0]
        assert row["year"] == 2026 and isinstance(row["year"], int)
        assert row["amount"] == 1.5 and isinstance(row["amount"], float)

    def test_missing_required_value_is_rejected(self, strict_contract):
        report = validate(
            strict_contract, self._rows({"code": "", "year": "2026", "amount": "1"})
        )
        assert not report.ok
        assert "required" in report.counts_by_rule()
        assert report.rows_ok == 0
        assert len(report.rejected_rows) == 1

    def test_bad_type_is_rejected(self, strict_contract):
        report = validate(
            strict_contract, self._rows({"code": "SG", "year": "not-a-year", "amount": "1"})
        )
        assert "type" in report.counts_by_rule()

    def test_range_checks(self, strict_contract):
        report = validate(
            strict_contract,
            self._rows(
                {"code": "SG", "year": "1999", "amount": "1"},
                {"code": "MY", "year": "2026", "amount": "-5"},
            ),
        )
        rules = report.counts_by_rule()
        assert rules.get("min") == 2  # year below 2015 and amount below 0

    def test_regex_check(self, strict_contract):
        report = validate(
            strict_contract, self._rows({"code": "SGP", "year": "2026", "amount": "1"})
        )
        assert "regex" in report.counts_by_rule()

    def test_allowed_values_check(self, strict_contract):
        report = validate(
            strict_contract,
            self._rows({"code": "SG", "year": "2026", "amount": "1", "band": "X"}),
        )
        assert "allowed" in report.counts_by_rule()

    def test_duplicate_grain_is_rejected(self, strict_contract):
        report = validate(
            strict_contract,
            self._rows(
                {"code": "SG", "year": "2026", "amount": "1"},
                {"code": "SG", "year": "2026", "amount": "2"},
            ),
        )
        assert "grain_unique" in report.counts_by_rule()
        assert report.rows_ok == 1  # the first one is kept

    def test_optional_null_is_fine(self, strict_contract):
        report = validate(
            strict_contract, self._rows({"code": "SG", "year": "2026", "amount": "1", "band": ""})
        )
        assert report.ok
        assert report.clean_rows[0]["band"] is None

    def test_rejected_rows_carry_a_reason(self, strict_contract):
        report = validate(
            strict_contract, self._rows({"code": "sg", "year": "2026", "amount": "1"})
        )
        assert "bad format" in report.rejected_rows[0]["_reject_reason"]

    def test_violation_list_is_capped_but_counts_are_not(self, strict_contract):
        rows = self._rows(*[{"code": "xx", "year": "2026", "amount": "1"} for _ in range(40)])
        report = validate(strict_contract, rows, max_violations_per_rule=5)
        detail = [v for v in report.violations if v.rule == "regex" and v.row_index >= 0]
        assert len(detail) == 5
        assert any("and 35 more" in v.message for v in report.violations)

    def test_pass_rate(self, strict_contract):
        report = validate(
            strict_contract,
            self._rows(
                {"code": "SG", "year": "2026", "amount": "1"},
                {"code": "bad", "year": "2026", "amount": "1"},
            ),
        )
        assert report.pass_rate == 0.5
        assert "50.0%" in report.summary()

    def test_empty_input(self, strict_contract):
        report = validate(strict_contract, [])
        assert report.ok and report.pass_rate == 1.0
