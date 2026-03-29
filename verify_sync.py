import json
import os
import sys
import ftplib

from google.oauth2 import service_account
from googleapiclient.discovery import build


REQUIRED_ENVS = [
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GDRIVE_FOLDER_ID",
    "FTP_HOST",
    "FTP_USER",
    "FTP_PASSWORD",
]


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: environment variable {name} is required.")
        sys.exit(1)
    return value


def get_drive_service(service_account_json: str):
    info = json.loads(service_account_json)
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def list_drive_folder_files(service, folder_id: str):
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None
    files = []

    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def list_ftp_files(host: str, user: str, password: str, port: int, directory: str):
    with ftplib.FTP() as ftp:
        ftp.connect(host, port)
        ftp.login(user, password)
        ftp.cwd(directory)
        names = ftp.nlst()
    return names


def main():
    missing = [name for name in REQUIRED_ENVS if name not in os.environ or not os.environ[name].strip()]
    if missing:
        print("ERROR: Missing required environment variables:")
        for name in missing:
            print(f"  - {name}")
        sys.exit(1)

    google_service_account_json = require_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    folder_id = require_env("GDRIVE_FOLDER_ID")
    ftp_host = require_env("FTP_HOST")
    ftp_user = require_env("FTP_USER")
    ftp_password = require_env("FTP_PASSWORD")
    ftp_dir = os.environ.get("FTP_DIR", "/")
    ftp_port = int(os.environ.get("FTP_PORT", "21"))

    print("Authenticating to Google Drive API...")
    drive_service = get_drive_service(google_service_account_json)

    print(f"Listing files in Drive folder {folder_id}...")
    drive_files = list_drive_folder_files(drive_service, folder_id)
    drive_names = {item["name"] for item in drive_files}

    print(f"Connecting to FTP {ftp_host}:{ftp_port} and listing {ftp_dir}...")
    ftp_files = list_ftp_files(ftp_host, ftp_user, ftp_password, ftp_port, ftp_dir)

    ftp_count = len(ftp_files)
    already_in_drive = sum(1 for name in ftp_files if name in drive_names)
    new_uploads = ftp_count - already_in_drive

    print("\n=== Dry-run verification summary ===")
    print(f"FTP files found: {ftp_count}")
    print(f"Files already present in Drive: {already_in_drive}")
    print(f"Files that would be new uploads: {new_uploads}")

    if ftp_count > 0:
        print("\nFTP file names:")
        for name in ftp_files:
            print(f"  - {name}")

    if drive_files:
        print("\nDrive file names:")
        for item in drive_files:
            print(f"  - {item['name']}")


if __name__ == "__main__":
    main()
