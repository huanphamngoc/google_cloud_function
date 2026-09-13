"""Turn raw sheet values into staging records.

Pure functions - this is the step that decides what actually gets written, so
it is kept free of I/O and covered directly by tests.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from scd2_schema import coerce, raw_json, row_hash


class TabData(object):
    """The result of reading and typing one tab."""

    def __init__(self, columns, records, coercion_errors, rows_read, rows_skipped):
        #: Source column names, in sheet order, after normalisation.
        self.columns = list(columns)
        #: Staging rows: typed values plus `_raw`, `_row_hash`, `_source_row`.
        self.records = records
        #: column -> count of values that could not be cast to the column type.
        self.coercion_errors = coercion_errors
        self.rows_read = rows_read
        self.rows_skipped = rows_skipped

    @property
    def row_count(self):
        return len(self.records)


def is_blank_row(row: Sequence[Any]) -> bool:
    return all(str(cell or "").strip() == "" for cell in row)


def build_records(
    header: Sequence[str],
    rows: Sequence[Sequence[Any]],
    schema: Mapping[str, str],
    exclude_columns: Sequence[str] = (),
    hash_columns: Sequence[str] = None,
) -> TabData:
    """Type every cell, hash every row, and keep the original alongside.

    Excluded columns are dropped from the typed output but remain in `_raw`
    and are left out of the hash - so a volatile column nobody wants to
    version (a "last edited by" stamp, say) stops generating a new version on
    every run without its value being lost.
    """
    exclude = set(exclude_columns)
    kept = [c for c in header if c not in exclude]
    hash_cols = list(hash_columns) if hash_columns is not None else kept

    records: List[Dict[str, Any]] = []
    errors: Dict[str, int] = {}
    skipped = 0

    for offset, row in enumerate(rows):
        if is_blank_row(row):
            skipped += 1
            continue

        record: Dict[str, Any] = {}
        for position, column in enumerate(header):
            if column in exclude:
                continue
            cell = row[position] if position < len(row) else ""
            value, ok = coerce(cell, schema.get(column, "STRING"))
            if not ok:
                errors[column] = errors.get(column, 0) + 1
            record[column] = value

        record["_raw"] = raw_json(header, row)
        record["_row_hash"] = row_hash(record, hash_cols)
        # +2: sheet rows are 1-based and the header occupies the first one, so
        # this is the row number a person sees in the spreadsheet UI.
        record["_source_row"] = offset + 2
        records.append(record)

    return TabData(
        columns=kept,
        records=records,
        coercion_errors=errors,
        rows_read=len(rows),
        rows_skipped=skipped,
    )


def find_duplicate_keys(
    records: Sequence[Mapping[str, Any]], key_columns: Sequence[str]
) -> List[Tuple[tuple, List[int]]]:
    """Locate business keys that appear more than once, with their sheet rows.

    Reported with row numbers because "duplicate key" is only actionable if
    the person fixing the sheet is told where to look.
    """
    if not key_columns:
        return []
    seen: Dict[tuple, List[int]] = {}
    for record in records:
        key = tuple(record.get(c) for c in key_columns)
        seen.setdefault(key, []).append(record.get("_source_row"))
    return [(key, rows) for key, rows in seen.items() if len(rows) > 1]


def missing_key_rows(
    records: Sequence[Mapping[str, Any]], key_columns: Sequence[str]
) -> List[int]:
    """Rows where any key column is empty.

    A NULL business key cannot be matched on the next run, so such a row would
    insert a fresh version every single time. Refused rather than versioned.
    """
    if not key_columns:
        return []
    return [
        record.get("_source_row")
        for record in records
        if any(record.get(c) is None for c in key_columns)
    ]
