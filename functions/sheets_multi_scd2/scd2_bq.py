"""BigQuery operations.

Everything that talks to BigQuery lives behind this class, so the orchestration
in `main.py` can be read without knowing the client library, and so a fake can
stand in for it in tests.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: Staging tables are rebuilt on every run and are worthless the moment the
#: merge commits. One day of retention is enough to inspect a failed load.
STAGING_EXPIRATION_MS = 24 * 60 * 60 * 1000


class BigQuery(object):
    """Thin wrapper over google-cloud-bigquery."""

    def __init__(self, project: Optional[str] = None, location: str = "asia-southeast1"):
        from google.cloud import bigquery  # type: ignore

        self._bq = bigquery
        self.client = bigquery.Client(project=project)
        self.project = self.client.project
        self.location = location

    # -- datasets ----------------------------------------------------------

    def ensure_dataset(self, dataset: str, default_table_expiration_ms=None) -> None:
        from google.api_core.exceptions import Conflict  # type: ignore

        ref = "%s.%s" % (self.project, dataset)
        obj = self._bq.Dataset(ref)
        obj.location = self.location
        if default_table_expiration_ms:
            obj.default_table_expiration_ms = default_table_expiration_ms
        try:
            self.client.create_dataset(obj)
        except Conflict:
            pass  # already there, which is the common case

    # -- tables ------------------------------------------------------------

    def table_schema(self, dataset: str, table: str) -> Optional[Dict[str, str]]:
        """Return column -> type for an existing table, or None if absent."""
        from google.api_core.exceptions import NotFound  # type: ignore

        try:
            obj = self.client.get_table("%s.%s.%s" % (self.project, dataset, table))
        except NotFound:
            return None
        return dict((field.name, field.field_type) for field in obj.schema)

    def load_staging(
        self,
        dataset: str,
        table: str,
        records: Sequence[Mapping[str, Any]],
        schema: Mapping[str, str],
    ) -> int:
        """Replace the staging table with this run's rows.

        WRITE_TRUNCATE is correct here and only here: staging holds one run's
        input, is rebuilt from the source every time, and nothing reads it
        after the merge. The versioned table it feeds is never truncated.
        """
        fields = [self._bq.SchemaField(name, t) for name, t in schema.items()]
        fields += [
            self._bq.SchemaField("_raw", "STRING"),
            self._bq.SchemaField("_row_hash", "STRING"),
            self._bq.SchemaField("_source_row", "INT64"),
        ]
        job_config = self._bq.LoadJobConfig(
            schema=fields,
            write_disposition=self._bq.WriteDisposition.WRITE_TRUNCATE,
            create_disposition=self._bq.CreateDisposition.CREATE_IF_NEEDED,
        )
        target = "%s.%s.%s" % (self.project, dataset, table)
        job = self.client.load_table_from_json(
            list(records), target, job_config=job_config, location=self.location
        )
        job.result()
        return job.output_rows or 0

    # -- sql ---------------------------------------------------------------

    def execute(self, sql: str, params: Optional[Mapping[str, Any]] = None):
        """Run a statement or multi-statement script and wait for it."""
        job_config = self._bq.QueryJobConfig()
        if params:
            job_config.query_parameters = [
                self._scalar_param(name, value) for name, value in params.items()
            ]
        job = self.client.query(sql, job_config=job_config, location=self.location)
        return job.result()

    def query_rows(
        self, sql: str, params: Optional[Mapping[str, Any]] = None
    ) -> List[dict]:
        return [dict(row) for row in self.execute(sql, params)]

    def _scalar_param(self, name: str, value: Any):
        import datetime as dt

        if isinstance(value, bool):
            kind = "BOOL"
        elif isinstance(value, int):
            kind = "INT64"
        elif isinstance(value, float):
            kind = "FLOAT64"
        elif isinstance(value, dt.datetime):
            kind = "TIMESTAMP"
        else:
            kind = "STRING"
        return self._bq.ScalarQueryParameter(name, kind, value)

    # -- inserts -----------------------------------------------------------

    def insert_rows(self, dataset: str, table: str, rows: Sequence[Mapping[str, Any]]):
        """Streaming insert, used only for the ops log.

        The ops log is written even when the load itself failed, so it must not
        depend on a load job succeeding.
        """
        target = "%s.%s.%s" % (self.project, dataset, table)
        errors = self.client.insert_rows_json(target, list(rows))
        return errors
