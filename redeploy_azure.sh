#!/usr/bin/env bash
# Redeploy code changes to the already-existing Azure App Service.
# Use this for every update after the first deploy — deploy_azure.sh (the
# full provisioning script) only needs to run once, when the app/database/
# infra didn't exist yet.
#
# Run from the project root:
#   bash redeploy_azure.sh
set -euo pipefail

RG=mk-timekeeping-rg      # must match what you used in deploy_azure.sh
APP=mk-timekeeping        # must match what you used in deploy_azure.sh

echo "== Packaging code =="
# No internal cd — relies on already being run from the project root (see
# the instructions above); a hardcoded path previously guessed a trailing
# space in the folder name that isn't actually there.
rm -f ../mk-timekeeping-deploy.zip
zip -r ../mk-timekeeping-deploy.zip . \
  -x ".venv/*" -x ".git/*" -x "tms.db" -x "tms_demo.db*" -x "__pycache__/*" -x "*/__pycache__/*"

echo "== Deploying =="
az webapp deploy -g "$RG" -n "$APP" --src-path ../mk-timekeeping-deploy.zip --type zip

echo ""
echo "Done. New code is live at: https://${APP}.azurewebsites.net"
echo "Any new/changed database columns are added automatically on startup"
echo "(app/db.py's additive-only migration) — no manual DB step needed for"
echo "column additions. Renamed/dropped columns still need a one-off script."
