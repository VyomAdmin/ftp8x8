import os
import ftplib
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── FTP CONFIG ────────────────────────────────────────────────────────────────
FTP_HOST     = os.environ["FTP_HOST"]
FTP_PORT     = int(os.getenv("FTP_PORT", 21))
FTP_USER     = os.environ["FTP_USER"]
FTP_PASSWORD = os.environ["FTP_PASSWORD"]
FTP_DIR      = os.getenv("FTP_DIR", "/")          # Remote directory to scan

# ── GOOGLE DRIVE CONFIG ───────────────────────────────────────────────────────
GDRIVE_FOLDER_ID          = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # JSON string (env var)
GOOGLE_CREDENTIALS_FILE     = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")

# ── OPTIONAL: track uploaded files to avoid duplicates ────────────────────────
UPLOADED_LOG = os.getenv("UPLOADED_LOG", "/tmp/uploaded_files.log")


def get_drive_service():
    """Build Google Drive API service using a service account."""
    scopes = ["https://www.googleapis.com/auth/drive.file"]

    if GOOGLE_SERVICE_ACCOUNT_JSON:
        import json
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=scopes
        )

    return build("drive", "v3", credentials=creds)


def load_uploaded_log() -> set:
    """Return a set of filenames already uploaded."""
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG, "r") as f:
        return set(line.strip() for line in f if line.strip())


def save_to_uploaded_log(filename: str):
    """Append a filename to the uploaded log."""
    with open(UPLOADED_LOG, "a") as f:
        f.write(filename + "\n")


def list_ftp_files(ftp: ftplib.FTP) -> list[str]:
    """Return list of filenames in the configured FTP directory."""
    ftp.cwd(FTP_DIR)
    files = ftp.nlst()
    logger.info(f"Found {len(files)} file(s) in FTP dir '{FTP_DIR}'")
    return files


def download_file(ftp: ftplib.FTP, filename: str, local_path: str):
    """Download a single file from FTP to local_path."""
    with open(local_path, "wb") as f:
        ftp.retrbinary(f"RETR {filename}", f.write)
    logger.info(f"Downloaded '{filename}' → {local_path}")


def upload_to_drive(service, local_path: str, filename: str):
    """Upload a local file to the configured Google Drive folder."""
    file_metadata = {
        "name": filename,
        "parents": [GDRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, name")
        .execute()
    )
    logger.info(f"Uploaded '{filename}' to Drive — file id: {uploaded.get('id')}")
    return uploaded.get("id")


def run():
    logger.info("=== FTP → Google Drive sync started ===")
    already_uploaded = load_uploaded_log()
    drive_service = get_drive_service()

    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT)
        ftp.login(FTP_USER, FTP_PASSWORD)
        logger.info(f"Connected to FTP: {FTP_HOST}:{FTP_PORT}")

        files = list_ftp_files(ftp)

        with tempfile.TemporaryDirectory() as tmpdir:
            for filename in files:
                if filename in already_uploaded:
                    logger.info(f"Skipping '{filename}' (already uploaded)")
                    continue

                local_path = str(Path(tmpdir) / filename)
                try:
                    download_file(ftp, filename, local_path)
                    upload_to_drive(drive_service, local_path, filename)
                    save_to_uploaded_log(filename)
                except Exception as e:
                    logger.error(f"Failed to process '{filename}': {e}")

    logger.info("=== Sync completed ===")


if __name__ == "__main__":
    run()
