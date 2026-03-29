# ftp8x8 — FTP → Google Drive Daily Sync

Automatically fetches files from an FTP server and uploads them to a Google Drive folder.  
Runs as a **Google Cloud Run Job**, triggered daily by **Cloud Scheduler**.  
Deploys automatically via **GitHub Actions** on every push to `main`.

---

## Architecture

```
GitHub push to main
      │
      ▼
GitHub Actions CI/CD
      │  builds Docker image
      │  pushes to GCR
      │  deploys Cloud Run Job
      │  creates Cloud Scheduler trigger
      ▼
Cloud Scheduler (daily cron)
      │  triggers
      ▼
Cloud Run Job
      │  downloads files from FTP
      │  uploads to Google Drive
      ▼
Google Drive Folder
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Google Cloud project | With billing enabled |
| GCP Service Account (deploy) | `roles/run.admin`, `roles/storage.admin`, `roles/cloudscheduler.admin` |
| GCP Service Account (Drive) | Google Drive API enabled, shared with target Drive folder |
| GitHub repo secrets | See table below |

---

## GitHub Secrets to Configure

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|---|---|
| `GCP_PROJECT_ID` | Your GCP project ID |
| `GCP_REGION` | Cloud Run region, e.g. `us-central1` |
| `GCP_SA_KEY` | JSON key of the deploy service account |
| `GCP_SCHEDULER_SA` | Email of the scheduler service account |
| `FTP_HOST` | FTP server hostname |
| `FTP_PORT` | FTP port (default `21`) |
| `FTP_USER` | FTP username |
| `FTP_PASSWORD` | FTP password |
| `FTP_DIR` | Remote directory path, e.g. `/uploads` |
| `GDRIVE_FOLDER_ID` | Google Drive folder ID (from the URL) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON of Drive service account (single line) |

---

## Local Development

```bash
# 1. Clone the repo
git clone https://github.com/VyomAdmin/ftp8x8.git
cd ftp8x8

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment
cp .env.example .env
# Edit .env with your real credentials

# 5. Run
python main.py
```

---

## Deployment

Push to `main` — GitHub Actions handles the rest:

```bash
git add .
git commit -m "feat: initial setup"
git push origin main
```

The workflow will:
1. Build the Docker image
2. Push it to Google Container Registry
3. Deploy/update the Cloud Run Job
4. Create/update a Cloud Scheduler rule (runs daily at **2:00 AM IST**)

To change the schedule, edit `schedule` in `.github/workflows/deploy.yml`:
```yaml
--schedule "0 2 * * *"    # cron syntax: minute hour day month weekday
```

---

## Google Drive Setup

1. Create a folder in Google Drive
2. Copy the folder ID from the URL: `https://drive.google.com/drive/folders/<FOLDER_ID>`
3. Create a GCP service account with Google Drive API enabled
4. Share the Drive folder with the service account's email (Editor access)
5. Add the service account JSON to `GOOGLE_SERVICE_ACCOUNT_JSON` secret

---

## File Structure

```
ftp8x8/
├── main.py                         # Core sync logic
├── requirements.txt                # Python dependencies
├── Dockerfile                      # Container definition
├── .env.example                    # Environment variable template
├── .gitignore
├── README.md
└── .github/
    └── workflows/
        └── deploy.yml              # CI/CD pipeline
```
