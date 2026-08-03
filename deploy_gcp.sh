#!/usr/bin/env bash
# GCP deployment script for MK Timekeeping — Cloud Run + Cloud SQL (Postgres).
# Mirrors deploy_azure.sh's job: create everything from scratch and do the
# first deploy. Safe to migrate to Azure later — same app code, same
# Postgres — see the migration notes at the bottom of this file.
#
# Requires: gcloud CLI installed and `gcloud auth login` done, and a GCP
# project with billing enabled.
#
# Run from the project root:
#   cd /Users/Ganesh/Projects/mk-timekeeping-poc-main
#   bash deploy_gcp.sh
set -euo pipefail

# --- variables — edit these ---
PROJECT_ID="mk-timekeeping"        # confirmed created + billing linked 2026-08-01
REGION="asia-south1"               # Mumbai — change if your team's elsewhere
SERVICE="mk-timekeeping"           # Cloud Run service name — becomes part of the URL
SQL_INSTANCE="mk-timekeeping-pg"
SQL_DB="tms"
SQL_USER="tmsadmin"
SQL_PASSWORD="LOMK@123"   # no "!" (shell history-expansion) or '"'/backslash (breaks the YAML env file below) — "@" is fine here, it's percent-encoded automatically when building DATABASE_URL below
BUCKET_NAME="${PROJECT_ID}-avatars"          # GCS bucket name, must be globally unique

echo "== Setting active project =="
gcloud config set project "$PROJECT_ID"

echo "== Enabling required APIs (first run only, safe to rerun) =="
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com

echo "== Cloud SQL Postgres instance (this step takes several minutes) =="
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  # --edition=ENTERPRISE is required here (2026-08 discovery): GCP now
  # defaults new instances to the ENTERPRISE_PLUS edition, which only
  # supports the pricier N2-based tiers (db-perf-optimized-N-*). The
  # cheap, shared-core db-f1-micro tier below — right-sized for ~45
  # internal users — only exists under the classic ENTERPRISE edition, so
  # it must be requested explicitly or the create call is rejected.
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_16 \
    --edition=ENTERPRISE \
    --tier=db-f1-micro \
    --region="$REGION" \
    --storage-size=10
else
  echo "Instance $SQL_INSTANCE already exists, skipping create."
fi

gcloud sql databases create "$SQL_DB" --instance="$SQL_INSTANCE" 2>/dev/null || echo "DB $SQL_DB already exists."
gcloud sql users create "$SQL_USER" --instance="$SQL_INSTANCE" --password="$SQL_PASSWORD" 2>/dev/null || echo "User $SQL_USER already exists (password unchanged)."

echo "== GCS bucket for avatar uploads (persists across deploys, unlike Cloud Run's local disk) =="
gcloud storage buckets create "gs://${BUCKET_NAME}" --location="$REGION" 2>/dev/null || echo "Bucket already exists."

# The Compute Engine default service account is what Cloud Build uses to
# fetch uploaded source, the runtime identity Cloud Run uses to
# gcsfuse-mount the avatars bucket, AND the identity the Cloud SQL Auth
# Proxy sidecar uses to open the DB connection socket (2026-08-03
# discovery: none of these three are granted by default on a freshly
# created project anymore — https://docs.cloud.google.com/build/docs/cloud-build-service-account-updates).
# Missing any one of them aborts the container before the app process even
# starts (a failed volume mount, or the DB socket never getting created,
# both fail startup the same way) — all three bindings are idempotent,
# safe to rerun.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "== Granting Cloud Build source-fetch permission to ${COMPUTE_SA} =="
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/run.builder" \
  --condition=None >/dev/null

echo "== Granting Cloud SQL connect permission to ${COMPUTE_SA} =="
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/cloudsql.client" \
  --condition=None >/dev/null

echo "== Granting avatar bucket read/write to ${COMPUTE_SA} =="
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/storage.objectAdmin" >/dev/null

CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
# Unix-socket connection string via the Cloud SQL Auth Proxy sidecar Cloud
# Run attaches automatically when --add-cloudsql-instances is set below —
# psycopg recognizes host=/cloudsql/... as a socket directory, not a hostname.
# SQL_PASSWORD is percent-encoded here (2026-08-03 discovery) because a
# connection URL's user:password@host syntax breaks if the password itself
# contains an unencoded "@" (or "/", ":", "?", "#") — the parser can't tell
# where the password ends and the host begins. Encoding it just for this
# string keeps the real password (used for gcloud sql users
# create/set-password and any direct psql login) exactly as typed
# everywhere else.
SQL_PASSWORD_URLENC=$(SQL_PASSWORD="$SQL_PASSWORD" python3 -c "import os, urllib.parse; print(urllib.parse.quote(os.environ['SQL_PASSWORD'], safe=''))")
DATABASE_URL="postgresql+psycopg://${SQL_USER}:${SQL_PASSWORD_URLENC}@/${SQL_DB}?host=/cloudsql/${CONNECTION_NAME}"

SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
BOOTSTRAP_ADMINS="Deepthi Divakaran:Deepthi@mkimmigrationlaw.com,Steve Kennedy:Steve@mkimmigrationlaw.com,Norine:Norine@mkimmigrationlaw.com"

echo "== Deploying to Cloud Run (builds from source — no Dockerfile needed) =="
# No internal cd — relies on already being run from the project root (see
# the instructions above). A hardcoded absolute path here previously broke
# this script: it guessed a trailing space in the folder name that isn't
# actually there, which made `cd` fail and, under set -euo pipefail, abort
# the whole deploy immediately.

# --set-env-vars takes a single comma-separated KEY=VALUE,KEY=VALUE string —
# BOOTSTRAP_ADMINS's own value has commas in it (separating the three
# admins), which gcloud's parser misreads as more pairs, breaking on
# "Steve Kennedy:Steve@..." (no "="). A YAML env-vars file sidesteps this
# entirely — no delimiter clashes, and it also avoids DATABASE_URL/
# SECRET_KEY needing any shell-escaping of their own special characters.
ENV_FILE=$(mktemp /tmp/mk-timekeeping-env.XXXXXX.yaml)
trap 'rm -f "$ENV_FILE"' EXIT
cat > "$ENV_FILE" <<EOF
AUTH_MODE: "password"
SECRET_KEY: "${SECRET_KEY}"
DATABASE_URL: "${DATABASE_URL}"
AVATAR_UPLOAD_DIR: "/mnt/avatars"
BOOTSTRAP_ADMINS: "${BOOTSTRAP_ADMINS}"
EOF

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --no-invoker-iam-check \
  --add-cloudsql-instances "$CONNECTION_NAME" \
  --add-volume=name=avatars,type=cloud-storage,bucket="$BUCKET_NAME" \
  --add-volume-mount=volume=avatars,mount-path=/mnt/avatars \
  --env-vars-file="$ENV_FILE"
# --no-invoker-iam-check (2026-08-03 discovery), not --allow-unauthenticated:
# this org's Google Workspace has Domain Restricted Sharing enabled, which
# silently blocks granting the run.invoker role to allUsers — the effect
# --allow-unauthenticated tries to achieve. --no-invoker-iam-check makes
# the service public by disabling the IAM check itself instead of trying
# to grant a role DRS won't allow, so it works under that same policy.

echo ""
echo "Done. Find the live URL above (ends in .run.app) — check:"
echo "  <url>/healthz  ->  should return {\"status\":\"ok\"}"
echo "  <url>/signup   ->  Deepthi/Steve/Norine can claim their accounts here"

# ---------------------------------------------------------------------------
# Migrating to Azure later — nothing here is a one-way door:
#   1. Data:  gcloud sql export sql $SQL_INSTANCE gs://$BUCKET_NAME/dump.sql --database=$SQL_DB
#             then download it and `psql <azure-connection-string> < dump.sql`
#             against the Azure Postgres Flexible Server from deploy_azure.sh.
#   2. Avatars: `gcloud storage cp -r gs://$BUCKET_NAME/* <local-folder>`,
#             then upload that folder to wherever AVATAR_UPLOAD_DIR points
#             on Azure (e.g. via `az webapp ssh` or Azure Storage).
#   3. Code:  already in this same repo — just run deploy_azure.sh.
#   4. DNS:   repoint your custom domain from the .run.app URL to the
#             .azurewebsites.net URL once Azure is confirmed working.
# ---------------------------------------------------------------------------
