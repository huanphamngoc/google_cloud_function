#!/usr/bin/env bash
# Workload Identity Federation: let GitHub Actions deploy to GCP without a key.
#
# How it works: GitHub mints a short-lived OIDC token describing the workflow
# run (which repo, which branch, which actor). GCP is configured to trust that
# issuer, checks the claims against a condition, and swaps the token for a
# short-lived GCP credential. No service account key is ever created,
# downloaded, or stored in GitHub Secrets.
#
# Run once. Takes about 5 minutes.
set -euo pipefail

PROJECT_ID="elt-drive-to-bq"
REGION="asia-southeast1"
GH_OWNER="huanphamngoc"
GH_REPO="google_cloud_function"

POOL="github-pool"
PROVIDER="github-provider"
DEPLOYER_SA_NAME="github-actions-deployer"
DEPLOYER_SA="${DEPLOYER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="sheets-to-bq-loader@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo ">>> project ${PROJECT_ID} (${PROJECT_NUMBER})"

# ---------------------------------------------------------------------------
# 1. APIs needed by federation itself.
# ---------------------------------------------------------------------------
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  iam.googleapis.com

# ---------------------------------------------------------------------------
# 2. A dedicated deploy identity.
#    Separate from the function's runtime SA on purpose: this one may deploy
#    but must never be able to read the spreadsheet or write to BigQuery.
# ---------------------------------------------------------------------------
gcloud iam service-accounts create "$DEPLOYER_SA_NAME" \
  --display-name="GitHub Actions deployer" 2>/dev/null \
  || echo "deployer service account already exists, skipping"

# ---------------------------------------------------------------------------
# 3. The identity pool and the GitHub provider.
#
#    The attribute-condition is the security boundary, not a formality.
#    Without it, ANY GitHub repository in the world could present a token to
#    this provider and impersonate the deployer. Google now refuses to create
#    a github provider without one.
# ---------------------------------------------------------------------------
gcloud iam workload-identity-pools create "$POOL" \
  --location="global" \
  --display-name="GitHub Actions pool" 2>/dev/null \
  || echo "pool already exists, skipping"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location="global" \
  --workload-identity-pool="$POOL" \
  --display-name="GitHub" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository == '${GH_OWNER}/${GH_REPO}'" \
  2>/dev/null || echo "provider already exists, skipping"

# ---------------------------------------------------------------------------
# 4. Let exactly this repository impersonate the deployer.
#
#    attribute.repository pins it to one repo. Narrowing further to one branch
#    is possible (attribute.ref) but would block manual workflow_dispatch runs
#    from other branches, which is useful during setup.
# ---------------------------------------------------------------------------
PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GH_OWNER}/${GH_REPO}"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="$PRINCIPAL" >/dev/null

# Needed so the workflow can mint an OIDC token for the smoke test step.
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --member="$PRINCIPAL" >/dev/null

# ---------------------------------------------------------------------------
# 5. What the deployer is allowed to do.
#
#    These are the same three grants that caused 403s during the Cloud Build
#    setup - written down this time instead of being rediscovered from a red
#    build. Deploying a gen2 function needs actAs on TWO service accounts:
#    the one the function runs as, and the one the container build runs as.
# ---------------------------------------------------------------------------
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/cloudfunctions.developer" --condition=None >/dev/null

for TARGET_SA in "$RUNTIME_SA" "$COMPUTE_SA"; do
  gcloud iam service-accounts add-iam-policy-binding "$TARGET_SA" \
    --member="serviceAccount:${DEPLOYER_SA}" \
    --role="roles/iam.serviceAccountUser" >/dev/null
done

# Smoke test calls the function, so the deployer must be allowed to invoke it.
# Skipped silently if the function does not exist yet - re-run after the first
# successful deploy.
for FN in sheets-multi-scd2 sheet-to-bq-loader; do
  gcloud functions add-invoker-policy-binding "$FN" \
    --region="$REGION" \
    --member="serviceAccount:${DEPLOYER_SA}" >/dev/null 2>&1 \
    && echo ">>> invoker granted on ${FN}" \
    || echo ">>> ${FN} not deployed yet - re-run this script after the first deploy"
done

# ---------------------------------------------------------------------------
# 6. The two values the workflows need. Neither is a secret: the provider path
#    is useless without the attribute condition matching your repo, and the
#    service account email is not a credential.
# ---------------------------------------------------------------------------
cat <<EOF

=============================================================================
Put these in the workflow files (they are already filled in if you used the
same names - check .github/workflows/*.yml):

  WIF_PROVIDER: projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
  DEPLOYER_SA:  ${DEPLOYER_SA}

Nothing goes into GitHub Secrets. That is the point.
=============================================================================
EOF
