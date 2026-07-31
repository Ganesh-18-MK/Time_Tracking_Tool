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
#   cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main "
#   bash deploy_gcp.sh
set -euo pipefail

# --- variables — edit these ---
PROJECT_ID="mk-timekeeping"        # confirmed created + billing linked 2026-08-01
REGION="asia-south1"               # Mumbai — change if your team's elsewhere
SERVICE="mk-timekeeping"           # Cloud Run service name — becomes part of the URL
SQL_INSTANCE="mk-timekeeping-pg"
SQL_DB="tms"
SQL_USER="tmsadmin"
SQL_PASSWORD="ChangeMeUseARealPassword123"   # no "!" — avoids shell history-expansion issues
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
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_16 \
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

CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
# Unix-socket connection string via the Cloud SQL Auth Proxy sidecar Cloud
# Run attaches automatically when --add-cloudsql-instances is set below —
# psycopg recognizes host=/cloudsql/... as a socket directory, not a hostname.
DATABASE_URL="postgresql+psycopg://${SQL_USER}:${SQL_PASSWORD}@/${SQL_DB}?host=/cloudsql/${CONNECTION_NAME}"

SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
BOOTSTRAP_ADMINS="Deepthi Divakaran:Deepthi@mkimmigrationlaw.com,Steve Kennedy:Steve@mkimmigrationlaw.com,Norine:Norine@mkimmigrationlaw.com"

echo "== Deploying to Cloud Run (builds from source — no Dockerfile needed) =="
cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main "
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --add-cloudsql-instances "$CONNECTION_NAME" \
  --add-volume=name=avatars,type=cloud-storage,bucket="$BUCKET_NAME" \
  --add-volume-mount=volume=avatars,mount-path=/mnt/avatars \
  --set-env-vars "AUTH_MODE=password,SECRET_KEY=${SECRET_KEY},DATABASE_URL=${DATABASE_URL},AVATAR_UPLOAD_DIR=/mnt/avatars,BOOTSTRAP_ADMINS=${BOOTSTRAP_ADMINS}"

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
