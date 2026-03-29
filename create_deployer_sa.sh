#!/usr/bin/env bash

set -euo pipefail

if [[ -z "${GCP_PROJECT_ID:-}" ]]; then
  echo "ERROR: GCP_PROJECT_ID is not set."
  echo "Please export GCP_PROJECT_ID before running this script."
  exit 1
fi

PROJECT="${GCP_PROJECT_ID}"
SA_NAME="ftp8x8-deployer"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
KEY_FILE="deploy_sa_key.json"

echo "Using project: ${PROJECT}"
echo "Service account: ${SA_EMAIL}"

# Ensure gcloud is configured for the target project
gcloud config set project "${PROJECT}"

# Create the service account if it does not already exist.
if gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "Service account ${SA_EMAIL} already exists."
else
  echo "Creating service account ${SA_EMAIL}..."
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="ftp8x8 deployer" \
    --project "${PROJECT}"
fi

# Assign required roles to the service account.
ROLES=(
  "roles/run.admin"
  "roles/storage.admin"
  "roles/cloudscheduler.admin"
  "roles/iam.serviceAccountUser"
)

echo "Assigning roles to ${SA_EMAIL}..."
for ROLE in "${ROLES[@]}"; do
  echo "  - ${ROLE}"
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet
done

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
