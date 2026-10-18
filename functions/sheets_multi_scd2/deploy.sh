#!/usr/bin/env bash
# End-to-end setup for the multi-tab SCD2 loader.
#
# Reuses the service account and build-SA grants already in place from
# functions/sheet_to_bq_loader (see its deployment-log.md). Steps 1b and 2 are
# no-ops if you have already run that script; they are kept so this file works
# on a fresh project too.
#
# Run the whole thing the first time: bash deploy.sh
# Re-run just the deploy afterwards by commenting out steps 1-5.
set -euo pipefail

PROJECT_ID="elt-drive-to-bq"
REGION="asia-southeast1"
SA_NAME="sheets-to-bq-loader"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

FUNCTION_NAME="sheets-multi-scd2"
SPREADSHEET_ID="1ClGSnw5kawxRCr43qNUJr4IdiWe3ItH28tBq6CVgcfk"

# Three datasets, no medallion: versioned tables, this run's input, and the
# operational log. See README.md for why there is no bronze/silver/gold.
BQ_DATASET="core"
BQ_STAGING_DATASET="staging"
BQ_OPS_DATASET="ops"

SCHEDULE="0 6 * * MON-FRI"

gcloud config set project "$PROJECT_ID"

# ---------------------------------------------------------------------------
# 1. APIs.
# ---------------------------------------------------------------------------
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sheets.googleapis.com \
  bigquery.googleapis.com \
  cloudscheduler.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  iam.googleapis.com

# ---------------------------------------------------------------------------
# 1b. Build service account grants.
#     Cloud Build on this project actually builds as the DEFAULT COMPUTE
#     service account, not the dedicated cloudbuild one - both are granted
#     here because gcloud only tells you which is missing a role after a
#     build has already failed.
# ---------------------------------------------------------------------------
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

for BUILD_SA in "${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
                "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"; do
  for ROLE in roles/cloudbuild.builds.builder \
              roles/artifactregistry.writer \
              roles/logging.logWriter \
              roles/storage.objectViewer \
              roles/run.admin; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${BUILD_SA}" \
      --role="$ROLE" \
      --condition=None >/dev/null
  done
done

# ---------------------------------------------------------------------------
# 2. Runtime service account. No key is created or downloaded - the function
#    gets this identity attached at runtime and authenticates via ADC.
# ---------------------------------------------------------------------------
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Sheets to BigQuery loader" 2>/dev/null \
  || echo "service account already exists, skipping"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataEditor" --condition=None >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser" --condition=None >/dev/null

# ---------------------------------------------------------------------------
# 3. Datasets. The function creates these itself on first run, but creating
#    them here pins the location explicitly - a dataset's location cannot be
#    changed afterwards, so it is worth being deliberate about.
# ---------------------------------------------------------------------------
bq mk --dataset --location="$REGION" "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null || true
bq mk --dataset --location="$REGION" "${PROJECT_ID}:${BQ_OPS_DATASET}" 2>/dev/null || true
# Staging is rebuilt every run and worthless after the merge commits, so its
# tables expire themselves after a day.
bq mk --dataset --location="$REGION" \
  --default_table_expiration=86400 \
  "${PROJECT_ID}:${BQ_STAGING_DATASET}" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 4. MANUAL STEP - the one people forget, and no IAM role substitutes for it:
#    reading a spreadsheet is a Drive sharing permission, not a GCP role.
# ---------------------------------------------------------------------------
echo ">>> Share the spreadsheet with this address as Viewer: ${SA_EMAIL}"
echo ">>> https://docs.google.com/spreadsheets/d/${SPREADSHEET_ID}/edit"
read -p "Press enter once the sheet has been shared... " _

# ---------------------------------------------------------------------------
# 5. Check sources.yaml before deploying. Every tab you want versioned needs a
#    business_key; unlisted tabs load append-only in snapshot mode.
# ---------------------------------------------------------------------------
echo ">>> Tabs declared in sources.yaml:"
grep -E '^  [A-Za-z]' "$(dirname "$0")/sources.yaml" || true
read -p "Press enter if that matches the tabs you want versioned... " _

# ---------------------------------------------------------------------------
# 6. Deploy. 512Mi/540s rather than the single-tab loader's 256Mi/120s: this
#    one walks every tab in the file sequentially. If a run ever approaches
#    half the timeout, split it (see the ?tabs= parameter) or move to a Cloud
#    Run Job - the threshold is in design/elt-drive-to-bq-decisions.md.
# ---------------------------------------------------------------------------
gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source="$(dirname "$0")" \
  --entry-point=load_spreadsheet \
  --trigger-http \
  --no-allow-unauthenticated \
  --run-service-account="$SA_EMAIL" \
  --memory=512Mi \
  --timeout=540s \
  --set-env-vars="SPREADSHEET_ID=${SPREADSHEET_ID},BQ_DATASET=${BQ_DATASET},BQ_STAGING_DATASET=${BQ_STAGING_DATASET},BQ_OPS_DATASET=${BQ_OPS_DATASET},BQ_LOCATION=${REGION}"

FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" \
  --region="$REGION" --gen2 --format="value(serviceConfig.uri)")

# ---------------------------------------------------------------------------
# 7. Let the service account invoke its own function (gen2 runs on Cloud Run
#    underneath, so this is a run.invoker grant).
# ---------------------------------------------------------------------------
gcloud functions add-invoker-policy-binding "$FUNCTION_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${SA_EMAIL}" >/dev/null

# ---------------------------------------------------------------------------
# 8. Schedule. OIDC - Scheduler mints a short-lived token per invocation, no
#    static secret anywhere.
# ---------------------------------------------------------------------------
gcloud scheduler jobs create http "${FUNCTION_NAME}-schedule" \
  --location="$REGION" \
  --schedule="$SCHEDULE" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="$SA_EMAIL" \
  --oidc-token-audience="$FUNCTION_URL" \
  2>/dev/null || echo "scheduler job exists; use 'jobs update http' to change it"

# ---------------------------------------------------------------------------
# 9. Dry run first: reads and validates every tab, writes nothing. This is
#    where duplicate keys and missing business keys surface, before any table
#    has been created.
# ---------------------------------------------------------------------------
TOKEN=$(gcloud auth print-identity-token)
echo ">>> Dry run:"
curl -s -X POST -H "Authorization: Bearer ${TOKEN}" "${FUNCTION_URL}?dry_run=1"
echo
read -p "Press enter to run for real, or Ctrl-C to fix sources.yaml first... " _

echo ">>> Live run:"
curl -s -X POST -H "Authorization: Bearer ${TOKEN}" "$FUNCTION_URL"
echo
echo
echo ">>> What ran:"
echo "bq query --use_legacy_sql=false 'SELECT tab, status, rows_inserted, rows_closed, rows_deleted FROM \`${PROJECT_ID}.${BQ_OPS_DATASET}.load_runs\` ORDER BY run_ts DESC LIMIT 20'"
