#!/usr/bin/env bash
# Redeploy code changes to the already-existing Cloud Run service.
# Use this for every update after the first deploy — deploy_gcp.sh (the full
# provisioning script: Cloud SQL instance, GCS bucket, IAM grants) only
# needs to run once, when the app/database/infra didn't exist yet.
#
# Deliberately does NOT pass --env-vars-file / --set-env-vars: Cloud Run
# carries forward the previous revision's env vars (DATABASE_URL,
# SECRET_KEY, AUTH_MODE, BOOTSTRAP_ADMINS) automatically when you don't
# specify them (confirmed against Google's current docs, 2026-08-03).
# Re-running deploy_gcp.sh instead would work too, but it regenerates
# SECRET_KEY every time, which silently invalidates every signed-in
# session — everyone gets logged out for no reason. This script avoids that.
#
# Run from the project root:
#   bash redeploy_gcp.sh
set -euo pipefail

# --- must match what you used in deploy_gcp.sh ---
PROJECT_ID="mk-timekeeping"
REGION="asia-south1"
SERVICE="mk-timekeeping"
SQL_INSTANCE="mk-timekeeping-pg"
BUCKET_NAME="${PROJECT_ID}-avatars"

CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

echo "== Setting active project =="
gcloud config set project "$PROJECT_ID"

echo "== Rebuilding from source and deploying (env vars carried over automatically) =="
# No internal cd — relies on already being run from the project root (see
# the instructions above).
#
# --add-cloudsql-instances / --add-volume(-mount) / --no-invoker-iam-check
# are repeated here even though Cloud Run also carries these forward on
# their own — cheap and idempotent to restate, and it means this script
# stays correct even if a future revision was ever deployed without them.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --no-invoker-iam-check \
  --add-cloudsql-instances "$CONNECTION_NAME" \
  --add-volume=name=avatars,type=cloud-storage,bucket="$BUCKET_NAME" \
  --add-volume-mount=volume=avatars,mount-path=/mnt/avatars

echo ""
echo "Done. New code is live — find the URL via:"
echo "  gcloud run services describe $SERVICE --region $REGION --format='value(status.url)'"
echo "(the URL printed just above by this deploy may not be the real working"
echo "one — see deploy_gcp.sh's notes on the hash-based default URL.)"
echo "Any new/changed database columns are added automatically on startup"
echo "(app/db.py's additive-only migration + the ensure_* backfills in"
echo "app/util.py) — no manual DB step needed for column additions."
