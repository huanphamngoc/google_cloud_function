"""Schema primitives: BigQuery-flavoured types, a widening lattice, and inference.

The widening lattice is the thing the evolution engine reasons over. A change from
type A to type B is *safe* (auto-evolvable) exactly when every value expressible in
A is expressible in B without loss. Anything else is either a narrowing (needs a
human to confirm the data really fits) or incompatible (blocked).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


class DType(str, Enum):
    """The subset of BigQuery types a spreadsheet source can produce."""

    BOOL = "BOOL"
    INT64 = "INT64"
    NUMERIC = "NUMERIC"
    FLOAT64 = "FLOAT64"
    DATE = "DATE"
    DATETIME = "DATETIME"
    TIMESTAMP = "TIMESTAMP"
    STRING = "STRING"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


#: Direct edges in the widening graph. Transitive closure is computed below.
_WIDENING_EDGES: Dict[DType, Set[DType]] = {
    DType.BOOL: {DType.INT64, DType.STRING},
    DType.INT64: {DType.NUMERIC, DType.FLOAT64, DType.STRING},
    DType.NUMERIC: {DType.FLOAT64, DType.STRING},
    DType.FLOAT64: {DType.STRING},
    DType.DATE: {DType.DATETIME, DType.STRING},
    DType.DATETIME: {DType.TIMESTAMP, DType.STRING},
    DType.TIMESTAMP: {DType.STRING},
    DType.STRING: set(),
}


def _transitive_closure(edges: Dict[DType, Set[DType]]) -> Dict[DType, Set[DType]]:
    closure: Dict[DType, Set[DType]] = {k: set(v) for k, v in edges.items()}
    changed = True
    while changed:
        changed = False
        for node, reachable in closure.items():
            extra: Set[DType] = set()
            for nxt in reachable:
                extra |= closure.get(nxt, set())
            if not extra <= reachable:
                reachable |= extra
                changed = True
    return closure


WIDENS_TO: Dict[DType, Set[DType]] = _transitive_closure(_WIDENING_EDGES)


def is_widening(src: DType, dst: DType) -> bool:
    """True when every value of ``src`` survives a cast to ``dst`` unchanged."""
    return src != dst and dst in WIDENS_TO[src]


def is_narrowing(src: DType, dst: DType) -> bool:
    """True when ``dst`` is strictly less permissive than ``src``."""
    return src != dst and src in WIDENS_TO[dst]


def is_compatible(src: DType, dst: DType) -> bool:
    return src == dst or is_widening(src, dst) or is_narrowing(src, dst)


def common_supertype(a: DType, b: DType) -> DType:
    """Narrowest type that both ``a`` and ``b`` widen into. STRING always works."""
    if a == b:
        return a
    if is_widening(a, b):
        return b
    if is_widening(b, a):
        return a
    candidates = (WIDENS_TO[a] & WIDENS_TO[b]) or {DType.STRING}
    # prefer the candidate that is *not* reachable from any other candidate,
    # i.e. the narrowest common ancestor
    narrowest = min(candidates, key=lambda c: len(WIDENS_TO[c]), default=DType.STRING)
    return narrowest


# --------------------------------------------------------------------------
# value-level inference
# --------------------------------------------------------------------------

_TRUE = {"true", "yes", "y", "t", "1"}
_FALSE = {"false", "no", "n", "f", "0"}
_NULLISH = {"", "-", "n/a", "na", "null", "none", "#n/a", "#value!", "#ref!"}

_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE_FMTS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d")
_DATETIME_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")


def is_nullish(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _NULLISH


def _try_parse(text: str) -> DType:
    """Narrowest type that parses this single non-null value."""
    low = text.strip().lower()
    if low in _TRUE or low in _FALSE:
        return DType.BOOL
    cleaned = text.strip().replace(",", "")
    if _INT_RE.match(cleaned):
        return DType.INT64
    if _NUM_RE.match(cleaned):
        return DType.FLOAT64
    for fmt in _DATE_FMTS:
        try:
            _dt.datetime.strptime(text.strip(), fmt)
            return DType.DATE
        except ValueError:
            pass
    for fmt in _DATETIME_FMTS:
        try:
            _dt.datetime.strptime(text.strip(), fmt)
            return DType.DATETIME
        except ValueError:
            pass
    return DType.STRING


def infer_dtype(values: Iterable[Any]) -> DType:
    """Infer the narrowest type that holds every non-null value in ``values``.

    An all-null (or empty) column infers as STRING, which is the safe default:
    STRING widens from nothing, so a later value of any type will register as a
    narrowing and be surfaced rather than silently coerced.
    """
    seen: Optional[DType] = None
    for raw in values:
        if is_nullish(raw):
            continue
        parsed = _try_parse(str(raw))
        seen = parsed if seen is None else common_supertype(seen, parsed)
        if seen == DType.STRING:
            break
    return seen or DType.STRING


def coerce(value: Any, dtype: DType) -> Any:
    """Cast a raw cell to ``dtype``. Returns None for nullish input.

    Raises ValueError when the value cannot be represented, which is what turns
    a declared-vs-actual mismatch into a validation failure rather than a silent
    NULL.
    """
    if is_nullish(value):
        return None
    text = str(value).strip()
    if dtype == DType.STRING:
        return text
    if dtype == DType.BOOL:
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError("{!r} is not a boolean".format(value))
    if dtype == DType.INT64:
        cleaned = text.replace(",", "")
        if not _INT_RE.match(cleaned):
            raise ValueError("{!r} is not an integer".format(value))
        return int(cleaned)
    if dtype in (DType.FLOAT64, DType.NUMERIC):
        cleaned = text.replace(",", "").rstrip("%")
        if not _NUM_RE.match(cleaned):
            raise ValueError("{!r} is not a number".format(value))
        return float(cleaned)
    if dtype == DType.DATE:
        for fmt in _DATE_FMTS:
            try:
                return _dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise ValueError("{!r} is not a date".format(value))
    if dtype in (DType.DATETIME, DType.TIMESTAMP):
        for fmt in _DATETIME_FMTS + _DATE_FMTS:
            try:
                return _dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError("{!r} is not a timestamp".format(value))
    raise ValueError("unsupported dtype {}".format(dtype))


# --------------------------------------------------------------------------
# fields and schemas
# --------------------------------------------------------------------------


def normalise(name: str) -> str:
    """Canonical comparison form for a column name.

    Spreadsheet headers arrive as ``"Country Code"``, ``"country_code"`` and
    ``"COUNTRY  CODE "`` on different days. None of those is a schema change.
    """
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower())
    return cleaned.strip("_")


@dataclass(frozen=True)
class Field:
    name: str
    dtype: DType = DType.STRING
    required: bool = False
    aliases: Tuple[str, ...] = ()
    description: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    regex: Optional[str] = None
    allowed: Tuple[str, ...] = ()
    #: version at which this field entered the contract (0 = original)
    since_version: int = 0

    @property
    def all_names(self) -> Tuple[str, ...]:
        return (self.name,) + tuple(self.aliases)

    @property
    def normalised_names(self) -> Tuple[str, ...]:
        return tuple(normalise(n) for n in self.all_names)

    def with_alias(self, alias: str) -> "Field":
        if normalise(alias) in self.normalised_names:
            return self
        return replace(self, aliases=self.aliases + (alias,))


@dataclass(frozen=True)
class Schema:
    fields: Tuple[Field, ...] = ()

    def __iter__(self):
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def get(self, name: str) -> Optional[Field]:
        target = normalise(name)
        for f in self.fields:
            if target in f.normalised_names:
                return f
        return None

    def fingerprint(self) -> str:
        """Stable hash of the schema's shape.

        Order-insensitive by design: a reordered spreadsheet is not a new schema,
        because every mapping in this system is by name.
        """
        payload = sorted(
            "{}:{}:{}".format(normalise(f.name), f.dtype.value, int(f.required))
            for f in self.fields
        )
        return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": f.name,
                "type": f.dtype.value,
                "required": f.required,
                "aliases": list(f.aliases),
                "since_version": f.since_version,
            }
            for f in self.fields
        ]


def infer_schema(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> Schema:
    """Infer a Schema from a raw header row plus sampled data rows."""
    columns: List[Field] = []
    for idx, raw_name in enumerate(header):
        values = [r[idx] if idx < len(r) else None for r in rows]
        non_null = [v for v in values if not is_nullish(v)]
        columns.append(
            Field(
                name=normalise(raw_name),
                dtype=infer_dtype(values),
                # a column with no nulls in the sample is *evidence* of required,
                # never a declaration of it - only the contract declares required
                required=False,
            )
        )
    return Schema(tuple(columns))
