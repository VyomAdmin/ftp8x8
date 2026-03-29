#!/usr/bin/env bash

set -euo pipefail

REPO="VyomAdmin/ftp8x8"
CLOUD_RUN_JOB_NAME="ftp8x8-sync"
GIT_COMMIT_MESSAGE="chore: trigger first deploy"

open_url() {
  local url="$1"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  else
    echo "Unable to open browser automatically. Visit: $url"
  fi
}

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI is required. Please install and authenticate with gh auth login."
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required."
  exit 1
fi

printf 'Staging all changes...\n'
git add .

if git diff --cached --quiet; then
  printf 'No changes to commit.\n'
else
  git commit -m "$GIT_COMMIT_MESSAGE"
fi

git push origin main

printf 'Waiting for GitHub Actions run on main...\n'
run_id=$(gh run list --branch main --limit 1 --json databaseId --repo "$REPO" --jq '.[0].databaseId')
if [[ -z "$run_id" ]]; then
  echo "ERROR: unable to find pull request run ID."
  exit 1
fi

printf 'Tailing workflow logs for run %s...\n' "$run_id"
if gh run watch "$run_id" --repo "$REPO" --exit-status; then
  region="${GCP_REGION:-unknown}"
  printf '\nDeployment succeeded.\n'
  printf 'Cloud Run Job: %s\n' "$CLOUD_RUN_JOB_NAME"
  printf 'Region: %s\n' "$region"
  exit 0
else
  logs_url="https://github.com/${REPO}/actions/runs/${run_id}"

  failed_step=$(gh api "/repos/${REPO}/actions/runs/${run_id}/jobs" --jq '.jobs[] | select(.conclusion=="failure") | .steps[] | select(.conclusion=="failure") | .name' | head -n 1 || true)
  if [[ -z "$failed_step" ]]; then
    failed_step="<unknown>"
  fi

  printf '\nDeployment failed.\n'
  printf 'Failed step: %s\n' "$failed_step"
  printf 'Opening logs URL: %s\n' "$logs_url"
  open_url "$logs_url"
  exit 1
fi
