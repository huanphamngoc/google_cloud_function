"""Warehouse adapters.

The pipeline talks to this interface, never to BigQuery directly. That is what
lets the whole thing run in tests against SQLite with no cloud account, while
production uses the same code path against BigQuery.
"""

from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .schema import DType, Field, Schema


#: ops tables, created on first connect. Mirrors the `ops` dataset in §04.
OPS_TABLES: Dict[str, str] = {
    "run_log": """
        CREATE TABLE IF NOT EXISTS ops_run_log (
            load_id TEXT PRIMARY KEY, source_id TEXT, started_at TEXT,
            finished_at TEXT, status TEXT, rows_in INTEGER, rows_loaded INTEGER,
            detail TEXT)
    """,
    "schema_registry": """
        CREATE TABLE IF NOT EXISTS ops_schema_registry (
            source_id TEXT, fingerprint TEXT, contract_version INTEGER,
            first_seen_at TEXT, last_seen_at TEXT, schema_json TEXT,
            PRIMARY KEY (source_id, fingerprint))
    """,
    "schema_evolution_log": """
        CREATE TABLE IF NOT EXISTS ops_schema_evolution_log (
            load_id TEXT, source_id TEXT, decided_at TEXT, decision TEXT,
            from_version INTEGER, to_version INTEGER, change_type TEXT,
            column_name TEXT, detail TEXT, confidence REAL)
    """,
    "dq_results": """
        CREATE TABLE IF NOT EXISTS ops_dq_results (
            load_id TEXT, source_id TEXT, checked_at TEXT, rule TEXT,
            column_name TEXT, violations INTEGER, rows_in INTEGER, rows_ok INTEGER)
    """,
    "recon_results": """
        CREATE TABLE IF NOT EXISTS ops_recon_results (
            load_id TEXT, dataset TEXT, checked_at TEXT, match_rate REAL,
            threshold REAL, passed INTEGER, comparisons INTEGER,
            value_delta REAL, scorecard_json TEXT)
    """,
}


class Warehouse(ABC):
    """Minimal surface the pipeline needs. Deliberately small."""

    @abstractmethod
    def ensure_table(self, table: str, schema: Schema, extra_columns: Sequence[str] = ()) -> None:
        ...

    @abstractmethod
    def columns(self, table: str) -> List[str]:
        ...

    @abstractmethod
    def add_column(self, table: str, field: Field) -> None:
        ...

    @abstractmethod
    def widen_column(self, table: str, field: Field) -> None:
        ...

    @abstractmethod
    def delete_load(self, table: str, load_id: str) -> int:
        ...

    @abstractmethod
    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        ...

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        ...

    def sync_schema(self, table: str, schema: Schema) -> List[str]:
        """Bring ``table`` in line with ``schema``, returning the actions taken.

        Additive only: columns are added, types are widened, nothing is ever
        dropped or narrowed by the pipeline.
        """
        existing = set(self.columns(table))
        actions: List[str] = []
        for f in schema:
            if f.name not in existing:
                self.add_column(table, f)
                actions.append("added {} {}".format(f.name, f.dtype.value))
        return actions


# --------------------------------------------------------------------------
# SQLite - the local/test backend
# --------------------------------------------------------------------------

_SQLITE_TYPES: Dict[DType, str] = {
    DType.BOOL: "INTEGER",
    DType.INT64: "INTEGER",
    DType.NUMERIC: "REAL",
    DType.FLOAT64: "REAL",
    DType.DATE: "TEXT",
    DType.DATETIME: "TEXT",
    DType.TIMESTAMP: "TEXT",
    DType.STRING: "TEXT",
}

#: columns every landing table carries, whatever the source
LINEAGE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("load_id", "TEXT"),
    ("_ingested_at", "TEXT"),
    ("_row_hash", "TEXT"),
    ("_source_hash", "TEXT"),
    ("_contract_version", "INTEGER"),
    ("_raw", "TEXT"),
)


class SQLiteWarehouse(Warehouse):
    """A real, working warehouse - just a small one.

    Used by the test suite and by ``clearstate-ingest run --local`` so the whole
    pipeline, including schema evolution against a live table, can be exercised
    without a GCP project.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_ops()

    def _init_ops(self) -> None:
        cur = self.conn.cursor()
        for ddl in OPS_TABLES.values():
            cur.execute(ddl)
        self.conn.commit()

    # -- schema ------------------------------------------------------------
    def ensure_table(self, table: str, schema: Schema, extra_columns: Sequence[str] = ()) -> None:
        cols = ["{} {}".format(f.name, _SQLITE_TYPES[f.dtype]) for f in schema]
        cols += ["{} {}".format(name, typ) for name, typ in LINEAGE_COLUMNS]
        cols += list(extra_columns)
        sql = "CREATE TABLE IF NOT EXISTS {} ({})".format(table, ", ".join(cols))
        self.conn.execute(sql)
        self.conn.commit()

    def columns(self, table: str) -> List[str]:
        cur = self.conn.execute("PRAGMA table_info({})".format(table))
        return [r["name"] for r in cur.fetchall()]

    def add_column(self, table: str, field: Field) -> None:
        self.conn.execute(
            "ALTER TABLE {} ADD COLUMN {} {}".format(
                table, field.name, _SQLITE_TYPES[field.dtype]
            )
        )
        self.conn.commit()

    def widen_column(self, table: str, field: Field) -> None:
        """SQLite is dynamically typed, so widening is a no-op here.

        BigQuery needs a real ALTER; see BigQueryWarehouse.widen_column. The
        distinction is intentional - the pipeline calls the same method and each
        backend does what its engine requires.
        """
        return None

    # -- data --------------------------------------------------------------
    def delete_load(self, table: str, load_id: str) -> int:
        cur = self.conn.execute("DELETE FROM {} WHERE load_id = ?".format(table), (load_id,))
        self.conn.commit()
        return cur.rowcount

    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        available = set(self.columns(table))
        keys = [k for k in rows[0].keys() if k in available]
        placeholders = ", ".join("?" for _ in keys)
        sql = "INSERT INTO {} ({}) VALUES ({})".format(table, ", ".join(keys), placeholders)
        payload = [tuple(_adapt(r.get(k)) for k in keys) for r in rows]
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        cur = self.conn.execute(sql, tuple(params))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        if cur.description is None:
            # a write: commit so ops rows survive a reconnect
            self.conn.commit()
        return rows

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SQLiteWarehouse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _adapt(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)


# --------------------------------------------------------------------------
# BigQuery - the production backend
# --------------------------------------------------------------------------


class BigQueryWarehouse(Warehouse):  # pragma: no cover - requires a GCP project
    """Production adapter. Imports google-cloud-bigquery lazily.

    The two schema-update options below are the whole reason additive evolution
    is safe in BigQuery: ALLOW_FIELD_ADDITION for new columns, and
    ALLOW_FIELD_RELAXATION for REQUIRED -> NULLABLE.
    """

    SCHEMA_UPDATE_OPTIONS = ["ALLOW_FIELD_ADDITION", "ALLOW_FIELD_RELAXATION"]

    def __init__(self, project: str, dataset: str, location: str = "EU") -> None:
        try:
            from google.cloud import bigquery  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "BigQueryWarehouse needs google-cloud-bigquery - pip install -e '.[gcp]'"
            ) from exc
        self._bq = bigquery
        self.client = bigquery.Client(project=project)
        self.project = project
        self.dataset = dataset
        self.location = location

    def _ref(self, table: str) -> str:
        return "{}.{}.{}".format(self.project, self.dataset, table)

    def ensure_table(self, table: str, schema: Schema, extra_columns: Sequence[str] = ()) -> None:
        from google.api_core.exceptions import NotFound  # type: ignore

        ref = self._ref(table)
        try:
            self.client.get_table(ref)
            return
        except NotFound:
            pass
        fields = [self._bq.SchemaField(f.name, f.dtype.value, mode="NULLABLE") for f in schema]
        fields += [
            self._bq.SchemaField("load_id", "STRING"),
            self._bq.SchemaField("_ingested_at", "TIMESTAMP"),
            self._bq.SchemaField("_row_hash", "STRING"),
            self._bq.SchemaField("_source_hash", "STRING"),
            self._bq.SchemaField("_contract_version", "INT64"),
            self._bq.SchemaField("_raw", "JSON"),
        ]
        tbl = self._bq.Table(ref, schema=fields)
        tbl.time_partitioning = self._bq.TimePartitioning(field="_ingested_at")
        self.client.create_table(tbl)

    def columns(self, table: str) -> List[str]:
        return [f.name for f in self.client.get_table(self._ref(table)).schema]

    def add_column(self, table: str, field: Field) -> None:
        tbl = self.client.get_table(self._ref(table))
        new_schema = list(tbl.schema) + [
            self._bq.SchemaField(field.name, field.dtype.value, mode="NULLABLE")
        ]
        tbl.schema = new_schema
        self.client.update_table(tbl, ["schema"])

    def widen_column(self, table: str, field: Field) -> None:
        """BigQuery cannot widen a column in place; rewrite the table with a cast.

        Guarded by the evolution engine, which only ever asks for a widening that
        is lossless by the lattice in schema.py.
        """
        ref = self._ref(table)
        cols = self.columns(table)
        projected = ", ".join(
            "CAST({0} AS {1}) AS {0}".format(c, field.dtype.value) if c == field.name else c
            for c in cols
        )
        job = self.client.query(
            "CREATE OR REPLACE TABLE `{0}` AS SELECT {1} FROM `{0}`".format(ref, projected)
        )
        job.result()

    def delete_load(self, table: str, load_id: str) -> int:
        job = self.client.query(
            "DELETE FROM `{}` WHERE load_id = @load_id".format(self._ref(table)),
            job_config=self._bq.QueryJobConfig(
                query_parameters=[self._bq.ScalarQueryParameter("load_id", "STRING", load_id)]
            ),
        )
        job.result()
        return job.num_dml_affected_rows or 0

    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        errors = self.client.insert_rows_json(self._ref(table), [dict(r) for r in rows])
        if errors:
            raise RuntimeError("BigQuery insert failed: {}".format(errors))
        return len(rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run SQL, translating SQLite-style ``?`` placeholders to BigQuery ``@pN``.

        The runner writes its ops rows through this one method so the pipeline
        stays backend-agnostic; without the translation those writes would only
        work against SQLite.
        """
        job_config = None
        if params:
            bq_params = []
            out = []
            idx = 0
            for ch in sql:
                if ch == "?":
                    out.append("@p{}".format(idx))
                    bq_params.append(
                        self._bq.ScalarQueryParameter(
                            "p{}".format(idx), _bq_param_type(params[idx]), params[idx]
                        )
                    )
                    idx += 1
                else:
                    out.append(ch)
            sql = "".join(out)
            job_config = self._bq.QueryJobConfig(query_parameters=bq_params)
        job = self.client.query(sql, job_config=job_config)
        rows = job.result()
        return [dict(r) for r in rows] if job.statement_type == "SELECT" else []


def _bq_param_type(value: Any) -> str:  # pragma: no cover - production path
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    return "STRING"
