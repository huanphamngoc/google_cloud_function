#!/usr/bin/env bash
# Cloud Composer 2 environment for the Sheets -> BigQuery SCD2 DAG.
#
# ############################################################################
# COST WARNING - read before running.
#
# Composer bills for the environment EXISTING, not for DAG runs. The smallest
# environment is roughly 300-500 USD/month whether it runs one DAG or none.
# Everything else in this project sits inside the free tier at about 0 USD.
#
# Step 9 at the bottom deletes the environment. If this is being built to
# learn Airflow rather than to run production, run step 9 the same day.
# ############################################################################
#
# Prerequisite: functions/sheets_multi_scd2 is already deployed and working.
# This script orchestrates that function; it does not replace it.
set -euo pipefail

PROJECT_ID="elt-drive-to-bq"
REGION="asia-southeast1"
ENV_NAME="etl-orchestrator"

FUNCTION_NAME="sheets-multi-scd2"
COMPOSER_SA_NAME="composer-runner"
COMPOSER_SA="${COMPOSER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

BQ_DATASET="core"
BQ_OPS_DATASET="ops"
ALERT_EMAIL=""   # optional: fill in to get an email when a task fails

gcloud config set project "$PROJECT_ID"

# ---------------------------------------------------------------------------
# 1. APIs.
# ---------------------------------------------------------------------------
gcloud services enable \
  composer.googleapis.com \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com

# ---------------------------------------------------------------------------
# 2. A dedicated service account for the environment.
#    Separate from the function's runtime identity: Composer needs to INVOKE
#    the function and READ BigQuery, not to write to the tables. Sharing one
#    identity would hand the orchestrator write access it never uses.
# ---------------------------------------------------------------------------
gcloud iam service-accounts create "$COMPOSER_SA_NAME" \
  --display-name="Cloud Composer environment runner" 2>/dev/null \
  || echo "service account already exists, skipping"

# composer.worker is the baseline the environment itself needs to function.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPOSER_SA}" \
  --role="roles/composer.worker" --condition=None >/dev/null

# Read-only on BigQuery: the verify task queries, the report task queries.
# Neither writes, so neither gets dataEditor.
for ROLE in roles/bigquery.dataViewer roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPOSER_SA}" \
    --role="$ROLE" --condition=None >/dev/null
done

# ---------------------------------------------------------------------------
# 3. Let Composer invoke the function. Gen2 functions run on Cloud Run, so
#    this is a run.invoker grant scoped to that one service - not project-wide.
# ---------------------------------------------------------------------------
gcloud functions add-invoker-policy-binding "$FUNCTION_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${COMPOSER_SA}" >/dev/null

FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" \
  --region="$REGION" --gen2 --format="value(serviceConfig.uri)")
echo ">>> Function URL: ${FUNCTION_URL}"

# ---------------------------------------------------------------------------
# 4. Your own account needs actAs on that service account to create an
#    environment that runs as it.
# ---------------------------------------------------------------------------
gcloud iam service-accounts add-iam-policy-binding "$COMPOSER_SA" \
  --member="user:$(gcloud config get-value account)" \
  --role="roles/iam.serviceAccountUser" >/dev/null

# ---------------------------------------------------------------------------
# 5. Pick the newest available image rather than pinning a version that may
#    no longer be offered in this region.
# ---------------------------------------------------------------------------
IMAGE_VERSION=$(gcloud composer environments list-image-versions \
  --locations="$REGION" --format="value(imageVersionId)" \
  | grep "composer-2" | head -n 1)
echo ">>> Using image: ${IMAGE_VERSION}"

# ---------------------------------------------------------------------------
# 6. Create the environment. THIS TAKES 20-30 MINUTES and starts billing the
#    moment it succeeds.
# ---------------------------------------------------------------------------
echo ">>> About to create a Composer environment (~300-500 USD/month)."
read -p ">>> Press enter to continue, or Ctrl-C to stop... " _

gcloud composer environments create "$ENV_NAME" \
  --location="$REGION" \
  --image-version="$IMAGE_VERSION" \
  --environment-size=small \
  --service-account="$COMPOSER_SA" \
  --env-variables="SHEETS_FUNCTION_URL=${FUNCTION_URL},GCP_PROJECT_ID=${PROJECT_ID},BQ_DATASET=${BQ_DATASET},BQ_OPS_DATASET=${BQ_OPS_DATASET},BQ_LOCATION=${REGION},ALERT_EMAIL=${ALERT_EMAIL}"

# ---------------------------------------------------------------------------
# 7. Upload the DAG and the tab config it reads.
#    sources.yaml is copied from the function directory so both read the same
#    declaration - one file, no drift between what loads and what orchestrates.
# ---------------------------------------------------------------------------
HERE="$(cd "$(dirname "$0")" && pwd)"
cp "${HERE}/../functions/sheets_multi_scd2/sources.yaml" "${HERE}/sources.yaml"

gcloud composer environments storage dags import \
  --environment="$ENV_NAME" --location="$REGION" \
  --source="${HERE}/sheets_scd2_dag.py"

gcloud composer environments storage dags import \
  --environment="$ENV_NAME" --location="$REGION" \
  --source="${HERE}/sources.yaml"

# ---------------------------------------------------------------------------
# 8. Open the UI and trigger the DAG once by hand.
# ---------------------------------------------------------------------------
AIRFLOW_URI=$(gcloud composer environments describe "$ENV_NAME" \
  --location="$REGION" --format="value(config.airflowUri)")
echo
echo ">>> Airflow UI: ${AIRFLOW_URI}"
echo ">>> Wait ~2 minutes for the DAG to be parsed, then trigger it:"
echo
echo "gcloud composer environments run ${ENV_NAME} --location=${REGION} \\"
echo "  dags trigger -- sheets_to_bq_scd2"
echo
echo ">>> Watch it:"
echo "gcloud composer environments run ${ENV_NAME} --location=${REGION} \\"
echo "  dags list-runs -- -d sheets_to_bq_scd2"

# ---------------------------------------------------------------------------
# 9. DELETE THE ENVIRONMENT when you are done. This is the only thing that
#    stops the meter - pausing the DAG does not.
# ---------------------------------------------------------------------------
cat <<'EOF'

>>> To stop billing when finished:
    gcloud composer environments delete etl-orchestrator --location=asia-southeast1
    # then remove the leftover bucket it does not delete itself:
    gsutil ls | grep composer
EOF
