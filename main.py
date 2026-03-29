import os
import ftplib
import tempfile
import logging
import json
import warnings
from datetime import datetime, timedelta
from pathlib import Path

# ── Suppress warnings ─────────────────────────────────────────────────────────
warnings.filterwarnings("ignore")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# ── Imports ───────────────────────────────────────────────────────────────────
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
FTP_HOST     = os.environ["FTP_HOST"]
FTP_PORT     = int(os.getenv("FTP_PORT", 21))
FTP_USER     = os.environ["FTP_USER"]
FTP_PASSWORD = os.environ["FTP_PASSWORD"]

GDRIVE_FOLDER_ID            = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CREDENTIALS_FILE     = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")

UPLOADED_LOG = os.getenv("UPLOADED_LOG", "/tmp/uploaded_files.log")


# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_yesterday_folder() -> str:
    """Returns folder name for yesterday e.g. S20260328"""
    yesterday = datetime.utcnow() - timedelta(days=1)
    return yesterday.strftime("S%Y%m%d")


def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def load_uploaded_log() -> set:
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG) as f:
        return set(line.strip() for line in f if line.strip())


def save_to_uploaded_log(filename: str):
    with open(UPLOADED_LOG, "a") as f:
        f.write(filename + "\n")


def get_or_create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    """Get existing or create new folder in Google Drive, returns folder id."""
    query = (
        f"name='{folder_name}' and "
        f"'{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        logger.info(f"Drive folder '{folder_name}' exists — id: {files[0]['id']}")
        return files[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    logger.info(f"Created Drive folder '{folder_name}' — id: {folder['id']}")
    return folder["id"]


def upload_to_drive(service, local_path: str, filename: str, folder_id: str):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = service.files().create(
        body=file_metadata, media_body=media, fields="id, name"
    ).execute()
    logger.info(f"Uploaded '{filename}' → Drive id: {uploaded.get('id')}")
    return uploaded.get("id")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    logger.info("=== FTP → Google Drive sync started ===")

    yesterday_folder = get_yesterday_folder()
    logger.info(f"Target FTP folder: /{yesterday_folder}")

    already_uploaded = load_uploaded_log()
    drive_service = get_drive_service()

    # Get or create dated folder in Drive
    drive_subfolder_id = get_or_create_drive_folder(
        drive_service, yesterday_folder, GDRIVE_FOLDER_ID
    )

    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASSWORD)
        ftp.set_pasv(True)
        logger.info(f"Connected to FTP: {FTP_HOST}:{FTP_PORT}")

        # Navigate to yesterday's directory
        try:
            ftp.cwd(f"/{yesterday_folder}")
            logger.info(f"Entered FTP directory: /{yesterday_folder}")
        except ftplib.error_perm as e:
            logger.error(f"FTP directory '/{yesterday_folder}' not found: {e}")
            return

        # List only files, skip subdirectories
        items = []
        ftp.retrlines("LIST", items.append)

        files = []
        for item in items:
            parts = item.split()
            if parts and not item.startswith("d"):
                files.append(parts[-1])

        logger.info(f"Found {len(files)} file(s) in /{yesterday_folder}")

        with tempfile.TemporaryDirectory() as tmpdir:
            for filename in files:
                log_key = f"{yesterday_folder}/{filename}"

                if log_key in already_uploaded:
                    logger.info(f"Skipping '{filename}' (already uploaded)")
                    continue

                local_path = str(Path(tmpdir) / filename)
                try:
                    with open(local_path, "wb") as f:
                        ftp.retrbinary(f"RETR {filename}", f.write)
                    logger.info(f"Downloaded '{filename}'")

                    upload_to_drive(
                        drive_service, local_path, filename, drive_subfolder_id)
                    save_to_uploaded_log(log_key)

                except Exception as e:
                    logger.error(f"Failed to process '{filename}': {e}")

    logger.info(f"=== Sync completed for {yesterday_folder} ===")


if __name__ == "__main__":
    run()