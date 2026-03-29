import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import paramiko
from flask import Flask, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── FTP / SFTP CONFIG ─────────────────────────────────────────────────────────
FTP_HOST = os.environ["FTP_HOST"]
FTP_PORT = int(os.getenv("FTP_PORT", 22))
FTP_USER = os.environ["FTP_USER"]
FTP_PASSWORD = os.environ["FTP_PASSWORD"]
FTP_DIR = os.getenv("FTP_DIR", "/")

# ── GOOGLE DRIVE CONFIG ───────────────────────────────────────────────────────
GDRIVE_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")

# ── OPTIONAL: track uploaded files to avoid duplicates ────────────────────────
UPLOADED_LOG = os.getenv("UPLOADED_LOG", "/tmp/uploaded_files.log")

sync_status = {
    "running": False,
    "last_run": None,
    "last_result": "not_run",
}

app = Flask(__name__)


def get_drive_service():
    """Build Google Drive API service using a service account."""
    scopes = ["https://www.googleapis.com/auth/drive.file"]

    if GOOGLE_SERVICE_ACCOUNT_JSON:
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


def list_sftp_files(sftp) -> list[str]:
    """Return list of filenames in the configured SFTP directory."""
    files = sftp.listdir()
    logger.info(f"Found {len(files)} file(s) in SFTP dir '{FTP_DIR}'")
    return files


def download_file(sftp, filename: str, local_path: str):
    """Download a single file from SFTP to local_path."""
    remote_path = f"{FTP_DIR.rstrip('/')}/{filename}" if FTP_DIR != "/" else f"/{filename}"
    logger.info(f"Downloading '{remote_path}' to '{local_path}'")
    sftp.get(remote_path, local_path)


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


def run_sync():
    if sync_status["running"]:
        logger.info("Sync already running, skipping new request.")
        return

    sync_status["running"] = True
    sync_status["last_run"] = datetime.utcnow().isoformat() + "Z"
    sync_status["last_result"] = "running"

    try:
        already_uploaded = load_uploaded_log()
        drive_service = get_drive_service()

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            logger.info(f"Connecting to SFTP: {FTP_HOST}:{FTP_PORT}")
            ssh.connect(
                hostname=FTP_HOST,
                port=FTP_PORT,
                username=FTP_USER,
                password=FTP_PASSWORD,
                timeout=30,
            )
            sftp = ssh.open_sftp()
            try:
                if FTP_DIR:
                    sftp.chdir(FTP_DIR)

                files = list_sftp_files(sftp)

                with tempfile.TemporaryDirectory() as tmpdir:
                    for filename in files:
                        if filename in already_uploaded:
                            logger.info(f"Skipping '{filename}' (already uploaded)")
                            continue

                        local_path = str(Path(tmpdir) / filename)
                        try:
                            download_file(sftp, filename, local_path)
                            upload_to_drive(drive_service, local_path, filename)
                            save_to_uploaded_log(filename)
                        except Exception as e:
                            logger.error(f"Failed to process '{filename}': {e}")

            finally:
                sftp.close()
        finally:
            ssh.close()

        sync_status["last_result"] = "success"
    except Exception as exc:
        logger.exception("Sync failed")
        sync_status["last_result"] = f"failure: {exc}"
    finally:
        sync_status["running"] = False


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"}), 200


@app.route("/sync", methods=["POST"])
def start_sync():
    if sync_status["running"]:
        return jsonify({"error": "sync already in progress"}), 409

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    return jsonify({"status": "accepted"}), 202


@app.route("/status", methods=["GET"])
def status():
    return jsonify(sync_status), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
