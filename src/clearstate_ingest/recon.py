"""The generic reconciliation diff.

One implementation, parameterised by grain, measures and tolerance - the same
logic that design/architecture.md §07 expresses as BigQuery SQL, available in
Python so it can be unit-tested and run against a local backend.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


MATCH = "match"
VARIANCE = "variance"
MISSING_IN_NEW = "missing_in_new"
EXTRA_IN_NEW = "extra_in_new"


@dataclass
class RowDiff:
    key: Tuple[Any, ...]
    measure: str
    baseline: Optional[float]
    candidate: Optional[float]
    status: str

    @property
    def abs_diff(self) -> Optional[float]:
        if self.baseline is None or self.candidate is None:
            return None
        return self.candidate - self.baseline

    @property
    def pct_diff(self) -> Optional[float]:
        if self.baseline in (None, 0) or self.candidate is None:
            return None
        return (self.candidate - self.baseline) / self.baseline

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": list(self.key),
            "measure": self.measure,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "status": self.status,
            "abs_diff": self.abs_diff,
            "pct_diff": self.pct_diff,
        }


@dataclass
class ReconResult:
    dataset: str
    grain: Tuple[str, ...]
    measures: Tuple[str, ...]
    abs_tolerance: float
    rel_tolerance: float
    threshold: float
    diffs: List[RowDiff] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.diffs)

    @property
    def counts(self) -> Dict[str, int]:
        return dict(Counter(d.status for d in self.diffs))

    @property
    def matched(self) -> int:
        return self.counts.get(MATCH, 0)

    @property
    def match_rate(self) -> float:
        return 1.0 if self.total == 0 else self.matched / self.total

    @property
    def passed(self) -> bool:
        return self.match_rate >= self.threshold

    def value_delta(self, measure: Optional[str] = None) -> float:
        """Total candidate minus total baseline - the headline number for a deliverable."""
        base = cand = 0.0
        for d in self.diffs:
            if measure and d.measure != measure:
                continue
            base += d.baseline or 0.0
            cand += d.candidate or 0.0
        return cand - base

    def top_variances(self, n: int = 20) -> List[RowDiff]:
        scored = [d for d in self.diffs if d.status != MATCH]
        return sorted(scored, key=lambda d: abs(d.abs_diff or float("inf")), reverse=True)[:n]

    def by_dimension(self, dimension: str) -> Dict[Any, Dict[str, int]]:
        """Variance counts grouped by one grain column - how an analyst reads it."""
        if dimension not in self.grain:
            raise KeyError("{!r} is not part of the grain {}".format(dimension, self.grain))
        pos = self.grain.index(dimension)
        out: Dict[Any, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for d in self.diffs:
            out[d.key[pos]][d.status] += 1
        return {k: dict(v) for k, v in out.items()}

    def summary(self) -> str:
        head = "{}: match rate {:.2%} of {} comparisons - {}".format(
            self.dataset, self.match_rate, self.total, "PASS" if self.passed else "FAIL"
        )
        detail = ", ".join("{}={}".format(k, v) for k, v in sorted(self.counts.items()))
        return "{}\n  {}\n  value delta: {:+,.2f}".format(head, detail, self.value_delta())


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reconcile(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    grain: Sequence[str],
    measures: Sequence[str],
    abs_tolerance: float = 0.01,
    rel_tolerance: float = 0.001,
    threshold: float = 0.995,
    dataset: str = "dataset",
) -> ReconResult:
    """Full outer join on ``grain``, then compare each measure within tolerance.

    A value matches when it is inside *either* the absolute or the relative
    tolerance. Small numbers need the absolute band (a 0.1% relative tolerance on
    a value of 3 is meaningless); large ones need the relative band.
    """
    grain = tuple(grain)
    measures = tuple(measures)

    def index(rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[Any, ...], Mapping[str, Any]]:
        out: Dict[Tuple[Any, ...], Mapping[str, Any]] = {}
        for row in rows:
            key = tuple(row.get(g) for g in grain)
            out[key] = row
        return out

    base_idx = index(baseline)
    cand_idx = index(candidate)
    all_keys = sorted(set(base_idx) | set(cand_idx), key=lambda k: tuple(str(p) for p in k))

    result = ReconResult(
        dataset=dataset,
        grain=grain,
        measures=measures,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
        threshold=threshold,
    )

    for key in all_keys:
        b_row = base_idx.get(key)
        c_row = cand_idx.get(key)
        for measure in measures:
            b_val = _to_float(b_row.get(measure)) if b_row is not None else None
            c_val = _to_float(c_row.get(measure)) if c_row is not None else None

            if b_row is None:
                status = EXTRA_IN_NEW
            elif c_row is None:
                status = MISSING_IN_NEW
            elif b_val is None and c_val is None:
                status = MATCH
            elif b_val is None or c_val is None:
                status = VARIANCE
            else:
                allowed = max(abs_tolerance, abs(b_val) * rel_tolerance)
                status = MATCH if abs(c_val - b_val) <= allowed else VARIANCE

            result.diffs.append(RowDiff(key, measure, b_val, c_val, status))

    return result


def scorecard(result: ReconResult, top_n: int = 20) -> Dict[str, Any]:
    """The dict an analyst-facing report or ops.recon_results row is built from."""
    return {
        "dataset": result.dataset,
        "grain": list(result.grain),
        "measures": list(result.measures),
        "comparisons": result.total,
        "match_rate": round(result.match_rate, 6),
        "threshold": result.threshold,
        "passed": result.passed,
        "counts": result.counts,
        "value_delta": round(result.value_delta(), 4),
        "abs_tolerance": result.abs_tolerance,
        "rel_tolerance": result.rel_tolerance,
        "top_variances": [d.as_dict() for d in result.top_variances(top_n)],
    }
