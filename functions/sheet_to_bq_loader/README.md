# sheet_to_bq_loader

A minimal Cloud Function (2nd gen, HTTP-triggered) that loads one Google Sheet tab
into a BigQuery table. This is the "day one" version of ingestion — no schema
evolution, no quarantine, no reconciliation. Read
[When to graduate](#when-to-graduate-to-clearstate_ingest) below for where the
line is.

## Files

| File | Purpose |
|---|---|
| `main.py` | the function — read the sheet, coerce types, load to BigQuery |
| `requirements.txt` | pinned to major versions only, matching the rest of this repo |
| `deploy.sh` | every `gcloud`/`bq` command in order, numbered, with the one manual step called out |

## The nine steps, and why each one is there

1. **Enable APIs.** `cloudfunctions`, `run` and `cloudbuild`/`artifactregistry` because gen2 functions deploy as a Cloud Run service under the hood; `sheets` and `bigquery` for the actual work; `cloudscheduler` for the trigger.

2. **Create the service account.** No key file is created anywhere in this flow. Cloud Functions gen2 lets you attach a service account at deploy time (`--run-service-account`), and the code picks up its credentials automatically via Application Default Credentials (`google.auth.default()`). A downloadable JSON key is a long-lived secret that has to be rotated and can leak; ADC has neither problem.

3. **Grant BigQuery roles, scoped to the dataset.** `roles/bigquery.dataEditor` is granted on the `landing` dataset specifically (via `bq add-iam-policy-binding`), not the project. `roles/bigquery.jobUser` is project-level because running a load job isn't a per-dataset permission in BigQuery's model. This service account cannot read or write any other dataset.

4. **Share the Sheet with the service account — manually.** This is the step that has nothing to do with GCP IAM and trips people up because of it. Google Sheets access is a **Drive sharing permission**. The service account's email (`sheets-to-bq-loader@<project>.iam.gserviceaccount.com`) has to be added as a Viewer on the spreadsheet itself, in the Sheets UI, the same way you'd share it with a colleague. No `gcloud` command does this — `deploy.sh` stops and prompts for it explicitly rather than deploying something that will 403 on first run.

5. **Create the table with an explicit schema.** Letting BigQuery infer the schema from the first load is how a column silently becomes the wrong type. The schema here matches `main.py`'s `LoadJobConfig` exactly — if you add a column to one, add it to the other.

6. **Deploy with `--no-allow-unauthenticated`.** The function is not public. Only identities explicitly granted the invoker role (step 7) can call its URL.

7. **Grant invoker to the same service account.** Cloud Scheduler will call this function *as* that service account (step 8), so it needs permission to invoke what it's about to call.

8. **Schedule with OIDC, not a shared secret.** `--oidc-service-account-email` makes Cloud Scheduler mint a short-lived identity token on every call. There's no API key or webhook secret sitting in a config file to leak.

9. **Test manually before trusting the schedule.** `gcloud auth print-identity-token` gets you a token as yourself; the `curl` call proves the function, the IAM bindings and the sheet-sharing are all correct before waiting for 06:00 tomorrow to find out.

## Configuration

Set in `deploy.sh` before running, or update later with
`gcloud functions deploy ... --update-env-vars`:

| Variable | Meaning |
|---|---|
| `SPREADSHEET_ID` | from the sheet's URL: `.../spreadsheets/d/<THIS>/edit` |
| `SHEET_RANGE` | tab + range, e.g. `Sheet1!A1:B` — include a header row |
| `BQ_DATASET` / `BQ_TABLE` | destination |

## What this function does and doesn't do

**Does:** reads the declared range, normalises headers (`"Id"` → `id`), casts known columns to their BigQuery type (`id` → INTEGER, `name` stays STRING), skips (and logs) rows missing a required value or failing a cast, tags every row with `_load_id` and `_ingested_at`, and does a full-refresh load (`WRITE_TRUNCATE`) — the table always reflects the sheet's current state after a run. The current shape is `id, name`; change `CANONICAL_COLUMNS` / `INT_COLUMNS` / `FLOAT_COLUMNS` in `main.py` and the `LoadJobConfig` schema together if the sheet gains columns.

**Doesn't:** handle a renamed or added column (it silently ignores anything not in `CANONICAL_COLUMNS` — see below), quarantine ambiguous changes, reconcile against a baseline, or preserve history between runs (`WRITE_TRUNCATE` replaces the whole table every time).

## When to graduate to `clearstate_ingest`

This function is intentionally simple: one file, no shared package dependency, easy
to read start to finish. The moment any of the following is true, switch to the
full pipeline in [`src/clearstate_ingest/`](../../src/clearstate_ingest/) instead
of extending this file:

- **The sheet's shape has changed more than once.** This function's `CANONICAL_COLUMNS` tuple is a hardcoded contract with no evolution logic — an added column is silently dropped, a renamed one is silently treated as removed *and* added. `clearstate_ingest` classifies every change (AUTO / PROPOSE / BLOCK) and never guesses on a rename. See [`design/schema-evolution.md`](../../design/schema-evolution.md).
- **You need history, not just current state.** `WRITE_TRUNCATE` has no memory of yesterday. The full pipeline appends to `landing` with lineage columns and lets a view select the current version, so "what did this look like on the 5th" is answerable.
- **You need to validate against a deliverable.** This function has no reconciliation step. `clearstate_ingest.recon` is the generic diff engine described in `architecture.md` §07.
- **You're onboarding a second source.** This function is one Python file per sheet. `clearstate_ingest` sources are a YAML contract each — see [`sources/market_inputs.yaml`](../../sources/market_inputs.yaml) for the shape.

If graduating: `clearstate_ingest.extract.read_google_sheet` and
`clearstate_ingest.warehouse.BigQueryWarehouse` are the two pieces already written
for this — the Cloud Function would call `Pipeline.run(contract)` instead of the
logic in this file's `main.py`.
