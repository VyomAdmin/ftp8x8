#!/usr/bin/env bash

set -euo pipefail

if [[ -z "${GCP_PROJECT_ID:-}" ]]; then
  echo "ERROR: GCP_PROJECT_ID is not set."
  echo "Please export GCP_PROJECT_ID before running this script."
  exit 1
fi

PROJECT="${GCP_PROJECT_ID}"
SA_NAME="ftp8x8-drive-uploader"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
KEY_FILE="drive_sa_key.json"

echo "Using project: ${PROJECT}"
echo "Service account: ${SA_EMAIL}"

# Configure gcloud for the target project
gcloud config set project "${PROJECT}"

echo "Enabling Google Drive API..."
gcloud services enable drive.googleapis.com --project "${PROJECT}"

# Create the service account if needed.
if gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "Service account ${SA_EMAIL} already exists."
else
  echo "Creating service account ${SA_EMAIL}..."
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="ftp8x8 Drive uploader" \
    --project "${PROJECT}"
fi

# Generate and download a JSON key file.
if [[ -f "${KEY_FILE}" ]]; then
  echo "Removing existing key file ${KEY_FILE}."
  rm -f "${KEY_FILE}"
fi

echo "Creating service account key file ${KEY_FILE}..."
gcloud iam service-accounts keys create "${KEY_FILE}" \
  --iam-account "${SA_EMAIL}" \
  --project "${PROJECT}"

echo
 echo "Service account email: ${SA_EMAIL}"
