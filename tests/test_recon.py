"""The reconciliation diff — the gate that decides whether a dataset is migrated."""

from __future__ import annotations

import pytest

from clearstate_ingest import reconcile, scorecard
from clearstate_ingest.extract import read_csv
from clearstate_ingest.recon import EXTRA_IN_NEW, MATCH, MISSING_IN_NEW, VARIANCE

GRAIN = ["country_code", "year"]
MEASURES = ["value_usd"]


def rows(*triples):
    return [
        {"country_code": c, "year": y, "value_usd": v} for c, y, v in triples
    ]


def run(baseline, candidate, **kw):
    params = dict(grain=GRAIN, measures=MEASURES, abs_tolerance=0.01, rel_tolerance=0.001)
    params.update(kw)
    return reconcile(baseline, candidate, **params)


class TestStatusClassification:
    def test_identical_values_match(self):
        r = run(rows(("SG", 2026, 100.0)), rows(("SG", 2026, 100.0)))
        assert r.counts == {MATCH: 1}
        assert r.match_rate == 1.0

    def test_inside_relative_tolerance_matches(self):
        # 0.05% drift on a large number, tolerance is 0.1%
        r = run(rows(("SG", 2026, 1_000_000.0)), rows(("SG", 2026, 1_000_500.0)))
        assert r.counts == {MATCH: 1}

    def test_outside_relative_tolerance_is_a_variance(self):
        r = run(rows(("SG", 2026, 1_000_000.0)), rows(("SG", 2026, 1_050_000.0)))
        assert r.counts == {VARIANCE: 1}

    def test_absolute_tolerance_covers_small_numbers(self):
        # 0.1% of 3.00 is 0.003 - the absolute band is what makes this sane
        r = run(rows(("SG", 2026, 3.00)), rows(("SG", 2026, 3.005)))
        assert r.counts == {MATCH: 1}

    def test_row_only_in_baseline_is_missing_in_new(self):
        r = run(rows(("SG", 2026, 1.0)), [])
        assert r.counts == {MISSING_IN_NEW: 1}

    def test_row_only_in_candidate_is_extra_in_new(self):
        r = run([], rows(("SG", 2026, 1.0)))
        assert r.counts == {EXTRA_IN_NEW: 1}

    def test_null_on_one_side_only_is_a_variance(self):
        r = run(rows(("SG", 2026, 100.0)), rows(("SG", 2026, None)))
        assert r.counts == {VARIANCE: 1}

    def test_null_on_both_sides_matches(self):
        r = run(rows(("SG", 2026, None)), rows(("SG", 2026, None)))
        assert r.counts == {MATCH: 1}

    def test_string_numbers_are_compared_numerically(self):
        r = reconcile(
            [{"country_code": "SG", "year": "2026", "value_usd": "100.00"}],
            [{"country_code": "SG", "year": "2026", "value_usd": "100.00"}],
            grain=GRAIN,
            measures=MEASURES,
        )
        assert r.counts == {MATCH: 1}


class TestAggregates:
    def test_match_rate_and_gate(self):
        baseline = rows(*[("C{}".format(i), 2026, 100.0) for i in range(100)])
        candidate = rows(*[("C{}".format(i), 2026, 100.0 if i else 200.0) for i in range(100)])
        r = run(baseline, candidate, threshold=0.995)
        assert r.match_rate == 0.99
        assert r.passed is False

        r2 = run(baseline, baseline, threshold=0.995)
        assert r2.passed is True

    def test_value_delta_is_the_headline_number(self):
        r = run(rows(("SG", 2026, 100.0)), rows(("SG", 2026, 130.0)))
        assert r.value_delta() == pytest.approx(30.0)

    def test_top_variances_are_ordered_by_absolute_difference(self):
        r = run(
            rows(("A", 2026, 100.0), ("B", 2026, 100.0)),
            rows(("A", 2026, 110.0), ("B", 2026, 400.0)),
        )
        top = r.top_variances()
        assert top[0].key[0] == "B"
        assert top[0].pct_diff == pytest.approx(3.0)

    def test_missing_rows_sort_first_in_top_variances(self):
        r = run(rows(("A", 2026, 1.0), ("B", 2026, 100.0)), rows(("B", 2026, 150.0)))
        # a missing row has no computable difference; it is the worst outcome
        assert r.top_variances()[0].status == MISSING_IN_NEW

    def test_by_dimension(self):
        r = run(
            rows(("SG", 2026, 100.0), ("MY", 2026, 100.0)),
            rows(("SG", 2026, 100.0), ("MY", 2026, 500.0)),
        )
        grouped = r.by_dimension("country_code")
        assert grouped["SG"] == {MATCH: 1}
        assert grouped["MY"] == {VARIANCE: 1}

    def test_by_dimension_rejects_a_non_grain_column(self):
        r = run(rows(("SG", 2026, 1.0)), rows(("SG", 2026, 1.0)))
        with pytest.raises(KeyError):
            r.by_dimension("value_usd")

    def test_empty_comparison_is_a_pass(self):
        r = run([], [])
        assert r.total == 0 and r.match_rate == 1.0 and r.passed


class TestScorecard:
    def test_scorecard_shape(self):
        r = run(rows(("SG", 2026, 100.0)), rows(("SG", 2026, 150.0)))
        card = scorecard(r)
        assert card["comparisons"] == 1
        assert card["passed"] is False
        assert card["counts"] == {VARIANCE: 1}
        assert card["top_variances"][0]["pct_diff"] == pytest.approx(0.5)


class TestAgainstSampleData:
    """The four planted discrepancies in sample_data must each be caught."""

    def test_sample_reconciliation(self, sample, contract):
        baseline = read_csv(sample["baseline"]).as_dicts()
        candidate = read_csv(sample["candidate"]).as_dicts()
        r = reconcile(
            baseline,
            candidate,
            grain=contract.grain,
            measures=contract.recon.measures,
            abs_tolerance=contract.recon.abs_tolerance,
            rel_tolerance=contract.recon.rel_tolerance,
            threshold=contract.recon.match_rate_threshold,
            dataset=contract.source_id,
        )
        counts = r.counts
        assert counts[MATCH] == 16          # includes the 0.04% drift, inside tolerance
        assert counts[VARIANCE] == 1        # the 8.2% one
        assert counts[MISSING_IN_NEW] == 1  # IVD-LEGACY, only in the old sheet
        assert counts[EXTRA_IN_NEW] == 1    # SG/IVD-MOLEC/2025, only in the new model
        assert r.passed is False            # below the 99.5% gate, correctly

    def test_a_clean_candidate_passes_the_gate(self, sample, contract):
        candidate = read_csv(sample["candidate"]).as_dicts()
        r = reconcile(
            candidate,
            candidate,
            grain=contract.grain,
            measures=contract.recon.measures,
            threshold=contract.recon.match_rate_threshold,
        )
        assert r.passed and r.match_rate == 1.0
