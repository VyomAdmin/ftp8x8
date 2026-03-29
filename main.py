import json
import logging
import os
import tempfile
from pathlib import Path

import paramiko
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

FTP_HOST = os.environ['FTP_HOST']
FTP_PORT = int(os.getenv('FTP_PORT', 22))
FTP_USER = os.environ['FTP_USER']
FTP_PASSWORD = os.environ['FTP_PASSWORD']
FTP_DIR = os.getenv('FTP_DIR', '/')

GDRIVE_FOLDER_ID = os.environ['GDRIVE_FOLDER_ID']
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'service_account.json')
UPLOADED_LOG = os.getenv('UPLOADED_LOG', '/tmp/uploaded_files.log')


def get_drive_service():
    scopes = ['https://www.googleapis.com/auth/drive.file']
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    return build('drive', 'v3', credentials=creds)


def load_uploaded_log():
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG, 'r') as f:
        return set(line.strip() for line in f if line.strip())


def save_to_uploaded_log(filename):
    with open(UPLOADED_LOG, 'a') as f:
        f.write(filename + '\n')


def list_sftp_files(sftp):
    files = sftp.listdir()
    logger.info(f"Found {len(files)} file(s)")
    return files


def download_file(sftp, filename, local_path):
    remote_path = f"{FTP_DIR.rstrip('/')}/{filename}" if FTP_DIR != '/' else f"/{filename}"
    logger.info(f"Downloading '{remote_path}' to '{local_path}'")
    sftp.get(remote_path, local_path)


def upload_to_drive(service, local_path, filename):
    file_metadata = {'name': filename, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = (
        service.files()
        .create(body=file_metadata, media_body=media, fields='id')
        .execute()
    )
    logger.info(f"Uploaded '{filename}' to Drive id: {uploaded.get('id')}")
    return uploaded.get('id')


def run():
    logger.info('=== FTP → Google Drive sync started ===')
    already_uploaded = load_uploaded_log()
    drive_service = get_drive_service()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
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
                        logger.info(f"Skipping '{filename}'")
                        continue

                    local_path = str(Path(tmpdir) / filename)
                    try:
                        download_file(sftp, filename, local_path)
                        upload_to_drive(drive_service, local_path, filename)
                        save_to_uploaded_log(filename)
                    except Exception as e:
                        logger.error(f"Failed '{filename}': {e}")
        finally:
            sftp.close()
    finally:
        ssh.close()

    logger.info('=== Sync completed ===')


if __name__ == '__main__':
    run()
