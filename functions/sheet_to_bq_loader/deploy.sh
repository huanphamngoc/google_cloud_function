#!/usr/bin/env bash
# End-to-end setup: service account -> Sheet sharing reminder -> dataset/table ->
# deploy -> secure invocation -> schedule. Run each numbered block in order the
# first time; re-run individual blocks (e.g. just "6") on later changes.
#
# Fill these in first:
set -euo pipefail

PROJECT_ID="elt-drive-to-bq"
REGION="asia-southeast1"
SA_NAME="sheets-to-bq-loader"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

FUNCTION_NAME="sheet-to-bq-loader"
SPREADSHEET_ID="1ClGSnw5kawxRCr43qNUJr4IdiWe3ItH28tBq6CVgcfk"
SHEET_RANGE="Sheet1!A1:B"      # confirm this matches your tab's actual name
BQ_DATASET="bronze"
BQ_TABLE="brz_customer"

SCHEDULE="0 6 * * MON-FRI"     # 06:00 Mon-Fri, in the scheduler location's timezone

gcloud config set project "$PROJECT_ID"

# ---------------------------------------------------------------------------
# 1. Enable the APIs this function and its trigger need.
# ---------------------------------------------------------------------------
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sheets.googleapis.com \
  bigquery.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com

# ---------------------------------------------------------------------------
# 1b. Grant the CLOUD BUILD service account permission to build the function.
#     This is a different identity from --run-service-account (step 6) - that
#     one runs the deployed code, this one builds the container image before
#     the code ever runs. Projects created after ~April 2023 no longer get
#     this granted by default, and the build fails with "missing permission
#     on the build service account" until it is added explicitly.
#     https://cloud.google.com/functions/docs/troubleshooting#build-service-account
# ---------------------------------------------------------------------------
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer" \
  --condition=None

# Some projects (this one included) have Cloud Build configured to actually
# build as the DEFAULT COMPUTE service account instead of the dedicated Cloud
# Build one above - gcloud will tell you which one it's missing a role on when
# a build fails, but granting both up front avoids the round-trip.
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/cloudbuild.builds.builder" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/artifactregistry.writer" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/logging.logWriter" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/storage.objectViewer" \
  --condition=None

# ---------------------------------------------------------------------------
# 2. Create the service account the function will run as.
#    No key is created or downloaded - Cloud Functions gen2 attaches this
#    identity at runtime and the code authenticates via ADC.
# ---------------------------------------------------------------------------
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Sheets to BigQuery loader"

# ---------------------------------------------------------------------------
# 3. Grant BigQuery access.
#
#    Ideally this would be dataset-scoped (bq add-iam-policy-binding), not
#    project-wide, so the identity can write to `bronze` and nothing else.
#    In practice `bq add-iam-policy-binding` on a dataset requires a Google
#    Cloud allowlist feature not enabled on every project ("This feature
#    requires allowlisting") - so this grants dataEditor at the project level
#    via the standard Resource Manager IAM API instead, which has no such
#    gate. Tighten later via the Console (dataset -> Sharing -> Permissions,
#    which is not subject to the same CLI limitation) if project-wide
#    dataEditor is broader than you want.
# ---------------------------------------------------------------------------
bq mk --dataset --location="$REGION" "${PROJECT_ID}:${BQ_DATASET}" || true

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataEditor" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser" \
  --condition=None

# ---------------------------------------------------------------------------
# 4. MANUAL STEP - cannot be scripted, and it is the one people forget:
#    Sheets access is a Drive sharing permission, NOT a GCP IAM role. Open the
#    target spreadsheet in the browser, click Share, and add:
#
#        SA_EMAIL   (printed below)   as Viewer
#
#    Nothing above grants this. The function will get a 403 from the Sheets
#    API until this is done, no matter how the service account's IAM roles
#    are configured.
# ---------------------------------------------------------------------------
echo ">>> Share the spreadsheet with this address as Viewer: ${SA_EMAIL}"
read -p "Press enter once the sheet has been shared... " _

# ---------------------------------------------------------------------------
# 5. Create the destination table with an explicit schema (see main.py for the
#    same schema used by the load job - keep them in sync if either changes).
# ---------------------------------------------------------------------------
bq mk --table \
  "${PROJECT_ID}:${BQ_DATASET}.${BQ_TABLE}" \
  id:INTEGER,name:STRING,_load_id:STRING,_ingested_at:TIMESTAMP \
  || echo "table already exists, skipping"

# ---------------------------------------------------------------------------
# 6. Deploy the function. --no-allow-unauthenticated: only identities granted
#    invoker (step 7) can call it - not the public internet.
#
#    NOTE if copying this command out to paste directly into an interactive
#    shell rather than running the whole script: SHEET_RANGE contains "!",
#    and bash's history expansion (interactive shells only, on by default)
#    will mangle "!A1" into "event not found" even inside double quotes.
#    Running via `bash deploy.sh` is unaffected (history expansion is off in
#    non-interactive shells) - this only bites a direct paste.
# ---------------------------------------------------------------------------
gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source="$(dirname "$0")" \
  --entry-point=load_sheet_to_bq \
  --trigger-http \
  --no-allow-unauthenticated \
  --run-service-account="$SA_EMAIL" \
  --memory=256Mi \
  --timeout=120s \
  --set-env-vars="SPREADSHEET_ID=${SPREADSHEET_ID},SHEET_RANGE=${SHEET_RANGE},BQ_DATASET=${BQ_DATASET},BQ_TABLE=${BQ_TABLE}"

FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" \
  --region="$REGION" --gen2 --format="value(serviceConfig.uri)")

# ---------------------------------------------------------------------------
# 7. Let the same service account invoke the function (gen2 runs on Cloud Run
#    under the hood, so this is a run.invoker grant on that revision).
# ---------------------------------------------------------------------------
gcloud functions add-invoker-policy-binding "$FUNCTION_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${SA_EMAIL}"

# ---------------------------------------------------------------------------
# 8. Schedule it. OIDC auth - Scheduler mints a short-lived identity token as
#    this service account on every invocation, no static secret involved.
# ---------------------------------------------------------------------------
gcloud scheduler jobs create http "${FUNCTION_NAME}-schedule" \
  --location="$REGION" \
  --schedule="$SCHEDULE" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="$SA_EMAIL" \
  --oidc-token-audience="$FUNCTION_URL" \
  || echo "scheduler job already exists, skipping (use 'jobs update' to change the schedule)"

# ---------------------------------------------------------------------------
# 9. Test it now, without waiting for the schedule.
# ---------------------------------------------------------------------------
echo ">>> Invoking once as yourself to confirm it works:"
TOKEN=$(gcloud auth print-identity-token)
curl -s -X POST -H "Authorization: Bearer ${TOKEN}" "$FUNCTION_URL" | tee /dev/stderr
echo
echo ">>> Then check: bq query \"SELECT * FROM \\\`${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}\\\` LIMIT 10\""
