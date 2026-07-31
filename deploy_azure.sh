#!/usr/bin/env bash
# Azure deployment script for MK Timekeeping.
# Edit the variables below, then run:
#   cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main "
#   bash deploy_azure.sh
set -euo pipefail

# --- variables — edit these ---
RG=mk-timekeeping-rg
LOCATION=centralindia          # change if your firm/staff are elsewhere
PLAN=mk-timekeeping-plan
APP=mk-timekeeping             # must be globally unique — becomes <APP>.azurewebsites.net
PG_SERVER=mk-timekeeping-pg
PG_ADMIN=tmsadmin
PG_PASSWORD='ChangeMeUseARealPassword123'   # no "!" — avoids shell history-expansion issues
DB_NAME=tms

echo "== Resource group =="
az group create -n "$RG" -l "$LOCATION"

echo "== Postgres Flexible Server (this step takes a few minutes) =="
az postgres flexible-server create \
  -g "$RG" -n "$PG_SERVER" -l "$LOCATION" \
  --admin-user "$PG_ADMIN" --admin-password "$PG_PASSWORD" \
  --sku-name Standard_B1ms --tier Burstable \
  --storage-size 32 --version 16 \
  --public-access 0.0.0.0-255.255.255.255

az postgres flexible-server db create -g "$RG" -s "$PG_SERVER" -d "$DB_NAME"

echo "== App Service plan + Web App =="
az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku B1
az webapp create -g "$RG" -p "$PLAN" -n "$APP" --runtime "PYTHON:3.11"

echo "== Startup command =="
az webapp config set -g "$RG" -n "$APP" \
  --startup-file "gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 app.main:app"

echo "== App settings =="
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DATABASE_URL="postgresql+psycopg://${PG_ADMIN}:${PG_PASSWORD}@${PG_SERVER}.postgres.database.azure.com/${DB_NAME}?sslmode=require"

az webapp config appsettings set -g "$RG" -n "$APP" --settings \
  AUTH_MODE=password \
  SECRET_KEY="$SECRET_KEY" \
  DATABASE_URL="$DATABASE_URL" \
  AVATAR_UPLOAD_DIR="/home/data/avatars" \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true

echo "== Packaging code =="
cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main "
rm -f ../mk-timekeeping-deploy.zip
zip -r ../mk-timekeeping-deploy.zip . \
  -x ".venv/*" -x ".git/*" -x "tms.db" -x "tms_demo.db*" -x "__pycache__/*" -x "*/__pycache__/*"

echo "== Deploying =="
az webapp deploy -g "$RG" -n "$APP" --src-path ../mk-timekeeping-deploy.zip --type zip

echo ""
echo "Done. Check:  https://${APP}.azurewebsites.net/healthz"
echo "Sign up at:   https://${APP}.azurewebsites.net/signup"
