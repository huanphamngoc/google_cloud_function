"""Header normalisation, type inference, coercion, and row hashing.

Pure functions only - no BigQuery, no Sheets, no I/O. Everything here is
directly unit-testable without a cloud account, which is where most of the
behaviour that is easy to get wrong actually lives.

Design stance carried over from the schema-evolution work in `src/`: never
guess when a guess can be silently wrong. Ambiguous dates stay STRING, type
conflicts do not destroy the existing column, and every original cell value
survives in `_raw` regardless of what the typed column ends up holding.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# BigQuery types this module will ever infer. Ordered loosely from most to
# least specific; STRING is the universal fallback.
BOOL = "BOOL"
INT64 = "INT64"
FLOAT64 = "FLOAT64"
NUMERIC = "NUMERIC"
BIGNUMERIC = "BIGNUMERIC"
DATE = "DATE"
TIMESTAMP = "TIMESTAMP"
STRING = "STRING"

VALID_TYPES = frozenset(
    {BOOL, INT64, FLOAT64, NUMERIC, BIGNUMERIC, DATE, TIMESTAMP, STRING}
)

#: Type changes BigQuery can apply in place with ALTER COLUMN SET DATA TYPE.
#: Anything outside this set is NOT a widening BigQuery will perform, so the
#: existing column is left alone and offending values fall back to `_raw`.
WIDENABLE = frozenset(
    {
        (INT64, NUMERIC),
        (INT64, BIGNUMERIC),
        (INT64, FLOAT64),
        (NUMERIC, BIGNUMERIC),
        (NUMERIC, FLOAT64),
        (BIGNUMERIC, FLOAT64),
    }
)

#: Values accepted as booleans, lower-cased. Deliberately excludes "1"/"0" -
#: those are far more often integers, and reading a quantity column as BOOL is
#: the kind of silent corruption this module exists to avoid.
_TRUE_WORDS = frozenset({"true", "yes", "y"})
_FALSE_WORDS = frozenset({"false", "no", "n"})

_INT_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*$|^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*\.\d+$|^-?\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)

#: Prefix reserved for SCD2 bookkeeping columns. Normalisation strips leading
#: underscores, so no source column can ever be given a name in this namespace
#: - that is what keeps a sheet from shadowing `_valid_from` and friends.
META_PREFIX = "_"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Header normalisation
# ---------------------------------------------------------------------------


def normalise_column(raw: str) -> str:
    """Turn a spreadsheet header cell into a legal BigQuery column name.

    Accents are stripped rather than dropped ("Doanh thu" -> "doanh_thu",
    "Sô´ lu'o'ng" -> "so_luong") so Vietnamese headers produce readable names
    instead of a row of underscores.
    """
    text = unicodedata.normalize("NFKD", str(raw or ""))
    # Vietnamese d-with-stroke has no decomposition; handle it explicitly.
    text = text.replace("đ", "d").replace("Đ", "D")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "column"
    if text[0].isdigit():
        text = "col_" + text
    return text


def normalise_headers(cells: Sequence[str]) -> List[str]:
    """Normalise a header row, resolving blanks and duplicates deterministically.

    A blank header becomes `column_<position>` rather than being dropped - a
    column with data under an empty header is still data, and dropping it here
    would violate the guarantee that nothing disappears silently.
    """
    out: List[str] = []
    seen: Dict[str, int] = {}
    for index, cell in enumerate(cells):
        name = normalise_column(cell) if str(cell or "").strip() else "column_%d" % (index + 1)
        if name in seen:
            seen[name] += 1
            name = "%s_%d" % (name, seen[name])
        else:
            seen[name] = 1
        out.append(name)
    return out


def is_valid_identifier(name: str) -> bool:
    return bool(_IDENT_RE.match(name))


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------


def _looks_bool(v: str) -> bool:
    low = v.lower()
    return low in _TRUE_WORDS or low in _FALSE_WORDS


def infer_type(values: Sequence[Any]) -> str:
    """Infer a BigQuery type from raw cell strings.

    Only unambiguous formats are promoted off STRING:

    - DATE requires ISO `YYYY-MM-DD`. `01/02/2026` is NOT inferred, because
      day-first and month-first are both plausible and picking one silently
      reorders a year of data. Force it with a `types:` override in the config
      once a human has confirmed which it is.
    - BOOL requires true/false/yes/no words, never 1/0.
    - Thousand separators are accepted for INT64/FLOAT64 (`1,234.56`) because
      the Sheets API returns formatted strings, but only in the US grouping.
    """
    seen = [str(v).strip() for v in values if str(v).strip() != ""]
    if not seen:
        return STRING

    if all(_looks_bool(v) for v in seen):
        return BOOL
    if all(_INT_RE.match(v) for v in seen):
        return INT64
    if all(_INT_RE.match(v) or _FLOAT_RE.match(v) for v in seen):
        return FLOAT64
    if all(_DATE_RE.match(v) for v in seen):
        return DATE
    if all(_TIMESTAMP_RE.match(v) for v in seen):
        return TIMESTAMP
    return STRING


def infer_schema(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Infer a column -> BigQuery type map for a whole tab."""
    overrides = {normalise_column(k): v.upper() for k, v in (overrides or {}).items()}
    schema: Dict[str, str] = {}
    for position, column in enumerate(columns):
        if column in overrides:
            forced = overrides[column]
            if forced not in VALID_TYPES:
                raise ValueError(
                    "unknown type override %r for column %r" % (forced, column)
                )
            schema[column] = forced
            continue
        column_values = [row[position] if position < len(row) else "" for row in rows]
        schema[column] = infer_type(column_values)
    return schema


def resolve_type(existing: str, incoming: str) -> Tuple[str, bool]:
    """Reconcile an inferred type against the type already in the table.

    Returns `(type_to_use, needs_alter)`. The existing type always wins unless
    BigQuery can widen it in place - a table that has been STRING for six
    months does not get narrowed to INT64 because this week's sample happens
    to be all digits.
    """
    if existing == incoming:
        return existing, False
    if (existing, incoming) in WIDENABLE:
        return incoming, True
    return existing, False


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def coerce(value: Any, bq_type: str) -> Tuple[Any, bool]:
    """Cast one cell to its column type.

    Returns `(value, ok)`. A failed cast yields `(None, False)` - the typed
    column goes NULL, but the caller still writes the original text into
    `_raw`, so the value is recoverable and the failure is countable.
    """
    if value is None:
        return None, True
    text = str(value).strip()
    if text == "":
        return None, True

    try:
        if bq_type == STRING:
            return text, True
        if bq_type == BOOL:
            low = text.lower()
            if low in _TRUE_WORDS:
                return True, True
            if low in _FALSE_WORDS:
                return False, True
            return None, False
        if bq_type == INT64:
            return int(text.replace(",", "")), True
        if bq_type in (FLOAT64, NUMERIC, BIGNUMERIC):
            return float(text.replace(",", "")), True
        if bq_type == DATE:
            return _dt.date.fromisoformat(text).isoformat(), True
        if bq_type == TIMESTAMP:
            cleaned = text.replace("Z", "+00:00").replace(" ", "T", 1)
            return _dt.datetime.fromisoformat(cleaned).isoformat(), True
    except (ValueError, TypeError):
        return None, False
    return None, False


# ---------------------------------------------------------------------------
# Row hashing - the change-detection primitive SCD2 depends on
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 17 significant digits round-trips a float exactly, so 1.10 and 1.1
        # hash identically while 1.1 and 1.100000000000001 do not.
        return format(value, ".17g")
    return str(value)


def row_hash(record: Mapping[str, Any], columns: Sequence[str]) -> str:
    """Hash the business payload of one row.

    NULL/empty columns are omitted from the payload rather than hashed as a
    sentinel. That makes adding a new empty column a no-op for every existing
    row: without this, one new column would close and re-open every record in
    the table on the next run, producing a full history of changes that never
    actually happened.
    """
    parts: List[str] = []
    for column in sorted(columns):
        value = record.get(column)
        if value is None or value == "":
            continue
        parts.append("%s=%s" % (column, _canonical(value)))
    blob = "\x1f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def raw_json(header: Sequence[str], row: Sequence[Any]) -> str:
    """Serialise the untouched source row, including unmapped trailing cells.

    This is the escape hatch that makes every other decision in this module
    reversible: whatever the typed columns did with a value, the original text
    is still here.
    """
    payload: Dict[str, Any] = {}
    for index, cell in enumerate(row):
        key = header[index] if index < len(header) else "column_%d" % (index + 1)
        payload[key] = cell
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
