"""Row-level validation against a contract, and canonicalisation of source rows.

Validation runs *after* the evolution engine has decided the shape is acceptable,
so everything here is about values, not columns.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import Contract
from .evolution import EvolutionPlan
from .schema import coerce, is_nullish, normalise


@dataclass
class Violation:
    rule: str
    column: str
    row_index: int
    value: Any
    message: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "column": self.column,
            "row_index": self.row_index,
            "value": None if self.value is None else str(self.value)[:200],
            "message": self.message,
        }


@dataclass
class ValidationReport:
    source_id: str
    rows_in: int = 0
    rows_ok: int = 0
    violations: List[Violation] = field(default_factory=list)
    #: rows that passed every check, canonicalised and typed
    clean_rows: List[Dict[str, Any]] = field(default_factory=list)
    #: rows that failed, with the reason attached
    rejected_rows: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def pass_rate(self) -> float:
        return 1.0 if self.rows_in == 0 else self.rows_ok / self.rows_in

    def counts_by_rule(self) -> Dict[str, int]:
        return dict(Counter(v.rule for v in self.violations))

    def summary(self) -> str:
        if self.ok:
            return "{}: {} rows, all valid".format(self.source_id, self.rows_in)
        parts = ", ".join(
            "{}={}".format(k, v) for k, v in sorted(self.counts_by_rule().items())
        )
        return "{}: {}/{} rows valid ({:.1%}) - {}".format(
            self.source_id, self.rows_ok, self.rows_in, self.pass_rate, parts
        )


def canonicalise(
    rows: Sequence[Mapping[str, Any]], plan: EvolutionPlan
) -> List[Dict[str, Any]]:
    """Rename source columns to canonical names and stash the rest in ``_raw``.

    ``_raw`` is what makes a later adoption of a new column retroactive: the value
    was landed the first time it appeared, even though no contract column existed
    for it yet.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        mapped: Dict[str, Any] = {}
        raw: Dict[str, Any] = {}
        for src_name, value in row.items():
            key = normalise(src_name)
            canonical = plan.column_map.get(key)
            if canonical:
                mapped[canonical] = value
            else:
                raw[key] = value
            # every source column is preserved in _raw, mapped or not, so the
            # archive and the table agree
            raw.setdefault(key, value)
        mapped["_raw"] = json.dumps(raw, default=str, sort_keys=True)
        out.append(mapped)
    return out


def validate(
    contract: Contract,
    rows: Sequence[Mapping[str, Any]],
    max_violations_per_rule: int = 25,
) -> ValidationReport:
    """Type, range, pattern, allowed-value and grain-uniqueness checks."""
    report = ValidationReport(source_id=contract.source_id, rows_in=len(rows))
    per_rule: Counter = Counter()

    def record(rule: str, column: str, idx: int, value: Any, message: str) -> None:
        per_rule[rule] += 1
        if per_rule[rule] <= max_violations_per_rule:
            report.violations.append(Violation(rule, column, idx, value, message))

    seen_grain: Dict[Tuple[Any, ...], int] = {}

    for idx, row in enumerate(rows):
        row_errors: List[str] = []
        typed: Dict[str, Any] = {"_raw": row.get("_raw")}

        for f in contract.schema:
            value = row.get(f.name)

            if is_nullish(value):
                if f.required:
                    record("required", f.name, idx, value, "required column is null")
                    row_errors.append("{} is null".format(f.name))
                typed[f.name] = None
                continue

            try:
                cast = coerce(value, f.dtype)
            except ValueError as exc:
                record("type", f.name, idx, value, str(exc))
                row_errors.append("{} bad type".format(f.name))
                typed[f.name] = None
                continue
            typed[f.name] = cast

            if f.min is not None and isinstance(cast, (int, float)) and cast < f.min:
                record("min", f.name, idx, value, "{} < min {}".format(cast, f.min))
                row_errors.append("{} below min".format(f.name))
            if f.max is not None and isinstance(cast, (int, float)) and cast > f.max:
                record("max", f.name, idx, value, "{} > max {}".format(cast, f.max))
                row_errors.append("{} above max".format(f.name))
            if f.regex and not re.match(f.regex, str(cast)):
                record("regex", f.name, idx, value, "does not match {}".format(f.regex))
                row_errors.append("{} bad format".format(f.name))
            if f.allowed and str(cast) not in f.allowed:
                record(
                    "allowed",
                    f.name,
                    idx,
                    value,
                    "not one of {}".format(", ".join(f.allowed)),
                )
                row_errors.append("{} not allowed".format(f.name))

        if contract.grain:
            key = tuple(typed.get(g) for g in contract.grain)
            if all(k is not None for k in key):
                if key in seen_grain:
                    record(
                        "grain_unique",
                        ",".join(contract.grain),
                        idx,
                        key,
                        "duplicate of row {}".format(seen_grain[key]),
                    )
                    row_errors.append("duplicate grain")
                else:
                    seen_grain[key] = idx

        if row_errors:
            rejected = dict(typed)
            rejected["_reject_reason"] = "; ".join(row_errors)
            report.rejected_rows.append(rejected)
        else:
            report.rows_ok += 1
            report.clean_rows.append(typed)

    for rule, total in per_rule.items():
        shown = min(total, max_violations_per_rule)
        if total > shown:
            report.violations.append(
                Violation(
                    rule,
                    "*",
                    -1,
                    None,
                    "... and {} more '{}' violations".format(total - shown, rule),
                )
            )

    return report
