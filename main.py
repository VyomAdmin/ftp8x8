import os
import io
import json
import time
import logging
import warnings
import tempfile
import zipfile
import requests
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
CLIENT_ID                   = os.environ["8X8_CLIENT_ID"]
CLIENT_SECRET               = os.environ["8X8_CLIENT_SECRET"]
GDRIVE_FOLDER_ID            = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CREDENTIALS_FILE     = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
UPLOADED_LOG                = os.getenv("UPLOADED_LOG", "/tmp/uploaded_files.log")

TOKEN_URL = "https://api.8x8.com/oauth/v2/token"
BASE_URL  = "https://api.8x8.com/storage"
API_VER   = "v3"


# ── AUTH ──────────────────────────────────────────────────────────────────────
def get_access_token() -> str:
    """Step 1 — OAuth2 client credentials."""
    logger.info("Fetching 8x8 access token...")
    r = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    logger.info("Access token obtained successfully")
    return token


def auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


# ── 8x8 API ───────────────────────────────────────────────────────────────────
def get_regions(token: str) -> list:
    """Step 2 — get all regions for this account."""
    url = f"{BASE_URL}/us-east/{API_VER}/regions"
    r = requests.get(url, headers=auth_headers(token))
    r.raise_for_status()
    regions = r.json()
    logger.info(f"Regions found: {regions}")
    return regions


def find_objects(token: str, region: str, date_from: str, date_to: str) -> list:
    """Step 3 — find all objects for yesterday with full pagination."""
    all_objects = []
    page_key = 0
    limit = 100

    while True:
        url = (
            f"{BASE_URL}/{region}/{API_VER}/objects"
            f"?filter=createdTime=ge={date_from};createdTime=lt={date_to}"
            f"&sortField=createdTime&sortDirection=ASC"
            f"&pageKey={page_key}&limit={limit}"
        )
        logger.info(f"Fetching objects page {page_key} from region {region}...")
        r = requests.get(url, headers=auth_headers(token))
        r.raise_for_status()
        data = r.json()

        content = data.get("content", [])
        all_objects.extend(content)
        logger.info(
            f"Page {page_key}: {len(content)} objects "
            f"(total: {len(all_objects)}, lastPage: {data.get('lastPage')})"
        )

        if data.get("lastPage", True):
            break
        page_key = data.get("pageKey", page_key + limit)

    return all_objects


def create_bulk_download(token: str, region: str, object_ids: list) -> str:
    """Step 4 — request zip creation, returns zipName."""
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/start"
    r = requests.post(url, headers=auth_headers(token), json=object_ids)
    r.raise_for_status()
    zip_name = r.json()["zipName"]
    logger.info(f"Bulk download requested — zipName: {zip_name}")
    return zip_name


def wait_for_download_ready(token: str, region: str, zip_name: str) -> bool:
    """Step 5 — poll every 20s until DONE (max 20 min)."""
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/status/{zip_name}"
    for attempt in range(60):
        r = requests.get(url, headers=auth_headers(token))
        r.raise_for_status()
        status = r.json().get("status")
        logger.info(f"Zip status: {status} (attempt {attempt + 1}/60)")
        if status == "DONE":
            return True
        if status in ("ERROR", "FAILED"):
            logger.error(f"Zip creation failed: {status}")
            return False
        time.sleep(20)
    logger.error("Timed out waiting for zip")
    return False


def download_zip(token: str, region: str, zip_name: str, local_path: str):
    """Step 6 — stream download the zip file."""
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/{zip_name}"
    logger.info(f"Downloading zip from {url}...")
    r = requests.get(url, headers=auth_headers(token), stream=True)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    size_mb = Path(local_path).stat().st_size / (1024 * 1024)
    logger.info(f"Zip downloaded: {size_mb:.2f} MB")


# ── GOOGLE DRIVE ──────────────────────────────────────────────────────────────
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


def save_to_uploaded_log(key: str):
    with open(UPLOADED_LOG, "a") as f:
        f.write(key + "\n")


def get_or_create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    existing = results.get("files", [])
    if existing:
        logger.info(f"Drive folder '{folder_name}' exists — id: {existing[0]['id']}")
        return existing[0]["id"]
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


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    logger.info("=== 8x8 Cloud Storage → Google Drive sync started ===")

    # Date range — yesterday
    yesterday   = datetime.utcnow() - timedelta(days=1)
    date_from   = yesterday.strftime("%Y-%m-%dT00:00:00")
    date_to     = yesterday.strftime("%Y-%m-%dT23:59:59")
    folder_name = yesterday.strftime("S%Y%m%d")
    logger.info(f"Syncing: {folder_name} ({date_from} → {date_to})")

    already_uploaded = load_uploaded_log()
    drive_service    = get_drive_service()

    # Create dated folder in Drive
    drive_subfolder_id = get_or_create_drive_folder(
        drive_service, folder_name, GDRIVE_FOLDER_ID)

    # Step 1 — get token
    token = get_access_token()

    # Step 2 — get regions
    regions = get_regions(token)

    for region in regions:
        logger.info(f"── Processing region: {region} ──")

        # Step 3 — find objects
        objects = find_objects(token, region, date_from, date_to)
        logger.info(f"Total objects in {region}: {len(objects)}")

        if not objects:
            logger.info(f"No objects found for {folder_name} in {region}")
            continue

        # Skip already uploaded
        new_objects = [o for o in objects if o["id"] not in already_uploaded]
        logger.info(f"New objects to sync: {len(new_objects)}")

        if not new_objects:
            logger.info("All objects already uploaded, skipping region")
            continue

        # Build id → objectName map for logging
        id_to_name = {o["id"]: o.get("objectName", o["id"]) for o in new_objects}
        object_ids = list(id_to_name.keys())

        with tempfile.TemporaryDirectory() as tmpdir:
            # Step 4 — create bulk download
            zip_name = create_bulk_download(token, region, object_ids)

            # Step 5 — wait for DONE
            if not wait_for_download_ready(token, region, zip_name):
                logger.error(f"Skipping region {region} — zip not ready")
                continue

            # Step 6 — download zip
            zip_path = str(Path(tmpdir) / zip_name)
            download_zip(token, region, zip_name, zip_path)

            # Extract zip and upload each file to Drive
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = zf.namelist()
                logger.info(f"Zip contains {len(members)} file(s)")

                for member in members:
                    local_path = str(Path(tmpdir) / member)
                    zf.extract(member, tmpdir)
                    try:
                        upload_to_drive(
                            drive_service, local_path, member, drive_subfolder_id)
                        # Find matching object id and mark uploaded
                        obj_id = next(
                            (oid for oid, name in id_to_name.items()
                             if name == member), None)
                        if obj_id:
                            save_to_uploaded_log(obj_id)
                        else:
                            save_to_uploaded_log(f"{folder_name}/{member}")
                    except Exception as e:
                        logger.error(f"Failed to upload '{member}': {e}")

    logger.info(f"=== Sync completed for {folder_name} ===")


if __name__ == "__main__":
    run()