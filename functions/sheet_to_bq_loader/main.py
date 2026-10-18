"""Cloud Function (2nd gen, HTTP-triggered): load one Google Sheet tab into BigQuery.

Deliberately standalone - no dependency on the clearstate_ingest package, so it
deploys as a single small function. It follows the same lineage convention as the
rest of this repo (_load_id, _ingested_at) but does a full-refresh load
(WRITE_TRUNCATE), not the contract-driven schema evolution in
src/clearstate_ingest. See functions/sheet_to_bq_loader/README.md for when to
graduate from this to the full pipeline.

Auth: uses the function's runtime service account via Application Default
Credentials - no key file is created or stored. The same service account must
also be added as a Viewer on the target Google Sheet (Sheets sharing is a Drive
ACL, not a GCP IAM grant - see the README).
"""

from __future__ import annotations

import datetime
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Sequence

import functions_framework
import google.auth
from google.cloud import bigquery
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sheet_to_bq_loader")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# The column shape this function knows about. Anything in the sheet that isn't
# one of these is ignored - this function does not do schema evolution.
# For evolving sources, use src/clearstate_ingest instead (see the README).
CANONICAL_COLUMNS = ("id", "name")
INT_COLUMNS = {"id"}
FLOAT_COLUMNS: set = set()
REQUIRED_COLUMNS = {"id", "name"}


def _normalise_header(cell: str) -> str:
    return cell.strip().lower().replace(" ", "_").replace("-", "_")


def _coerce(raw: Optional[str], column: str) -> Any:
    text = "" if raw is None else str(raw).strip()
    if text == "" or text.lower() in ("n/a", "na", "-", "#n/a"):
        return None
    if column in INT_COLUMNS:
        return int(float(text.replace(",", "")))
    if column in FLOAT_COLUMNS:
        return float(text.replace(",", "").rstrip("%"))
    return text


def read_sheet(creds, spreadsheet_id: str, sheet_range: str) -> List[List[str]]:
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=sheet_range)
        .execute()
    )
    values = result.get("values", [])
    if not values:
        raise ValueError("no rows returned for range {!r}".format(sheet_range))
    return values


def to_records(
    values: Sequence[Sequence[str]], load_id: str, ingested_at: str
) -> List[Dict[str, Any]]:
    header = [_normalise_header(c) for c in values[0]]
    records: List[Dict[str, Any]] = []
    skipped = 0

    for raw_row in values[1:]:
        row = dict(zip(header, raw_row))
        record: Dict[str, Any] = {}
        for col in CANONICAL_COLUMNS:
            try:
                record[col] = _coerce(row.get(col), col)
            except ValueError:
                # a value that won't cast (e.g. "41200.5" for an INT64 column) -
                # skip the row rather than load a wrong number
                record = None
                break
        if record is None or any(record.get(c) is None for c in REQUIRED_COLUMNS):
            skipped += 1
            continue
        record["_load_id"] = load_id
        record["_ingested_at"] = ingested_at
        records.append(record)

    if skipped:
        logger.warning("skipped %d row(s) with a missing/invalid required value", skipped)
    return records


def load_to_bigquery(
    client: bigquery.Client, table_ref: str, records: List[Dict[str, Any]]
) -> None:
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[
            bigquery.SchemaField("id", "INTEGER"),
            bigquery.SchemaField("name", "STRING"),
            bigquery.SchemaField("_load_id", "STRING"),
            bigquery.SchemaField("_ingested_at", "TIMESTAMP"),
        ],
    )
    job = client.load_table_from_json(records, table_ref, job_config=job_config)
    job.result()  # raises on failure - a bad load must not report success


@functions_framework.http
def load_sheet_to_bq(request):
    """Entry point. Triggered by Cloud Scheduler (see the README for the OIDC setup)."""
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    sheet_range = os.environ.get("SHEET_RANGE", "Sheet1!A1:B")
    dataset = os.environ["BQ_DATASET"]
    table = os.environ["BQ_TABLE"]

    creds, project = google.auth.default(scopes=SHEETS_SCOPES)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", project)

    load_id = uuid.uuid4().hex
    ingested_at = datetime.datetime.utcnow().isoformat() + "Z"

    try:
        values = read_sheet(creds, spreadsheet_id, sheet_range)
        records = to_records(values, load_id, ingested_at)
        if not records:
            logger.warning("no valid rows to load - leaving the table untouched")
            return {"status": "ok", "rows_loaded": 0, "load_id": load_id}, 200

        client = bigquery.Client(project=project)
        table_ref = "{}.{}.{}".format(project, dataset, table)
        load_to_bigquery(client, table_ref, records)

    except Exception:
        logger.exception("load failed (load_id=%s)", load_id)
        return {"status": "error", "load_id": load_id}, 500

    logger.info("loaded %d row(s) into %s (load_id=%s)", len(records), table_ref, load_id)
    return {"status": "ok", "rows_loaded": len(records), "load_id": load_id}, 200
