#!/usr/bin/env bash

set -euo pipefail

REPO="VyomAdmin/ftp8x8"

required_env=(
  GCP_PROJECT_ID
  GCP_REGION
  GCP_SCHEDULER_SA
  FTP_HOST
  FTP_PORT
  FTP_USER
  FTP_PASSWORD
  FTP_DIR
  GDRIVE_FOLDER_ID
)

for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: $name is not set."
    exit 1
  fi
done

if [[ ! -f "deploy_sa_key.json" ]]; then
  echo "ERROR: deploy_sa_key.json not found in $(pwd)"
  exit 1
fi

if [[ ! -f "drive_sa_key.json" ]]; then
  echo "ERROR: drive_sa_key.json not found in $(pwd)"
  exit 1
fi

# Read and set deploy SA key
GCP_SA_KEY_CONTENT=$(<deploy_sa_key.json)
printf '%s' "$GCP_SA_KEY_CONTENT" | gh secret set GCP_SA_KEY --repo "$REPO"

# Minify drive SA key JSON and set both secret names
GCP_DRIVE_SA_KEY_CONTENT=$(python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)))' <drive_sa_key.json)
printf '%s' "$GCP_DRIVE_SA_KEY_CONTENT" | gh secret set GCP_DRIVE_SA_KEY --repo "$REPO"
printf '%s' "$GCP_DRIVE_SA_KEY_CONTENT" | gh secret set GOOGLE_SERVICE_ACCOUNT_JSON --repo "$REPO"

# Set env-variable-based secrets
for name in "${required_env[@]}"; do
  value="${!name}"
  printf '%s' "$value" | gh secret set "$name" --repo "$REPO"
done

echo "All secrets set"
