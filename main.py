import os
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
CLIENT_ID                   = os.environ["8X8_CLIENT_ID"]
CLIENT_SECRET               = os.environ["8X8_CLIENT_SECRET"]
GDRIVE_FOLDER_ID            = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CREDENTIALS_FILE     = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
UPLOADED_LOG                = os.getenv("UPLOADED_LOG", "/tmp/uploaded_files.log")

# Chunk size — max IDs per bulk download request
BULK_CHUNK_SIZE = 50

TOKEN_URL = "https://api.8x8.com/oauth/v2/token"
BASE_URL  = "https://api.8x8.com/storage"
API_VER   = "v3"


# ── AUTH ──────────────────────────────────────────────────────────────────────
def get_access_token():
    logger.info("Fetching 8x8 access token...")
    r = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30
    )
    r.raise_for_status()
    logger.info("Access token obtained")
    return r.json()["access_token"]


def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


# ── 8x8 API ───────────────────────────────────────────────────────────────────
def get_regions(token):
    """Step 2 — discover all regions."""
    r = requests.get(
        f"{BASE_URL}/us-east/{API_VER}/regions",
        headers=auth_headers(token), timeout=30
    )
    r.raise_for_status()
    regions = r.json()
    logger.info(f"Regions: {regions}")
    return regions


def find_objects_for_yesterday(token, region, date_from, date_to):
    """
    Step 3 — fetch all callcenterrecording objects, filter by date in Python.
    Uses type filter only (date filter returns 500 on this account).
    Sorts DESC so yesterday's records appear first and we stop early.
    """
    all_objects = []
    page_key    = 0
    limit       = 100
    from_dt     = datetime.strptime(date_from, "%Y-%m-%dT%H:%M:%S")
    to_dt       = datetime.strptime(date_to,   "%Y-%m-%dT%H:%M:%S")

    logger.info(f"Searching region={region} for {date_from} → {date_to}")

    while True:
        url = (
            f"{BASE_URL}/{region}/{API_VER}/objects"
            f"?filter=type==callcenterrecording"
            f"&sortField=createdTime&sortDirection=DESC"
            f"&pageKey={page_key}&limit={limit}"
        )
        try:
            r = requests.get(url, headers=auth_headers(token), timeout=60)
            if r.status_code != 200:
                logger.error(f"API {r.status_code}: {r.text[:300]}")
                break

            data    = r.json()
            content = data.get("content", [])
            stop_early = False

            for obj in content:
                created = obj.get("createdTime", "")
                try:
                    obj_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")
                    if from_dt <= obj_dt <= to_dt:
                        all_objects.append(obj)
                    elif obj_dt < from_dt:
                        stop_early = True
                        break
                except Exception:
                    continue

            logger.info(
                f"Page {page_key}: {len(content)} fetched, "
                f"{len(all_objects)} matched so far"
            )

            if stop_early or data.get("lastPage", True):
                break

            page_key = data.get("pageKey", page_key + limit)

        except Exception as e:
            logger.error(f"Error on page {page_key}: {e}")
            break

    logger.info(f"Total objects for {date_from[:10]}: {len(all_objects)}")
    return all_objects


def create_bulk_download(token, region, object_ids):
    """Step 4 — POST array of IDs → returns zipName."""
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/start"
    logger.info(f"Requesting bulk download for {len(object_ids)} files...")
    logger.info(f"IDs: {object_ids}")  # logs the array like your curl example

    r = requests.post(url, headers=auth_headers(token), json=object_ids, timeout=60)
    r.raise_for_status()
    zip_name = r.json()["zipName"]
    logger.info(f"Bulk download created — zipName: {zip_name}")
    return zip_name


def wait_for_zip(token, region, zip_name, poll_secs=20, max_attempts=60):
    """Step 5 — poll until DONE."""
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/status/{zip_name}"
    for attempt in range(1, max_attempts + 1):
        r = requests.get(url, headers=auth_headers(token), timeout=30)
        r.raise_for_status()
        status = r.json().get("status")
        logger.info(f"Zip status: {status} (attempt {attempt}/{max_attempts})")
        if status == "DONE":
            return True
        if status in ("ERROR", "FAILED"):
            logger.error(f"Zip failed: {status}")
            return False
        time.sleep(poll_secs)
    logger.error("Timed out waiting for zip")
    return False


def download_zip(token, region, zip_name, local_path):
    """Step 6 — stream download the zip."""
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/{zip_name}"
    logger.info(f"Downloading zip {zip_name}...")
    r = requests.get(url, headers=auth_headers(token), stream=True, timeout=300)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    size_mb = Path(local_path).stat().st_size / (1024 * 1024)
    logger.info(f"Zip saved: {size_mb:.1f} MB → {local_path}")


# ── GOOGLE DRIVE ──────────────────────────────────────────────────────────────
def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        info  = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def load_uploaded_log():
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG) as f:
        return set(line.strip() for line in f if line.strip())


def save_to_uploaded_log(key):
    with open(UPLOADED_LOG, "a") as f:
        f.write(key + "\n")


def get_or_create_drive_folder(service, folder_name, parent_id):
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results  = service.files().list(q=query, fields="files(id, name)").execute()
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


def upload_to_drive(service, local_path, filename, folder_id):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media         = MediaFileUpload(local_path, resumable=True)
    uploaded      = service.files().create(
        body=file_metadata, media_body=media, fields="id, name"
    ).execute()
    logger.info(f"Uploaded '{filename}' → Drive id: {uploaded.get('id')}")


def chunk_list(lst, size):
    """Split list into chunks of given size."""
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    logger.info("=== 8x8 Cloud Storage → Google Drive sync started ===")

    yesterday   = datetime.utcnow() - timedelta(days=1)
    date_from   = yesterday.strftime("%Y-%m-%dT00:00:00")
    date_to     = yesterday.strftime("%Y-%m-%dT23:59:59")
    folder_name = yesterday.strftime("S%Y%m%d")

    logger.info(f"Date: {folder_name} | {date_from} → {date_to}")

    already_uploaded = load_uploaded_log()
    drive_service    = get_drive_service()
    drive_folder_id  = get_or_create_drive_folder(
        drive_service, folder_name, GDRIVE_FOLDER_ID)

    token   = get_access_token()
    regions = get_regions(token)

    for region in regions:
        logger.info(f"══ Region: {region} ══")

        # Step 3 — find yesterday's objects
        objects = find_objects_for_yesterday(token, region, date_from, date_to)

        if not objects:
            logger.info(f"No objects found for {folder_name} in {region}")
            continue

        # Skip already uploaded
        new_objects = [o for o in objects if o["id"] not in already_uploaded]
        logger.info(f"New objects to download: {len(new_objects)} / {len(objects)}")

        if not new_objects:
            logger.info("All already uploaded, skipping region")
            continue

        # Build id → objectName map
        id_to_name = {
            o["id"]: o.get("objectName", f"{o['id']}.mp3")
            for o in new_objects
        }

        total_uploaded = 0
        total_failed   = 0

        # Process in chunks of BULK_CHUNK_SIZE
        chunks = list(chunk_list(list(id_to_name.keys()), BULK_CHUNK_SIZE))
        logger.info(f"Processing {len(new_objects)} files in {len(chunks)} chunk(s)")

        for chunk_num, id_chunk in enumerate(chunks, 1):
            logger.info(f"── Chunk {chunk_num}/{len(chunks)}: {len(id_chunk)} files ──")
            logger.info(f"IDs being requested: {id_chunk}")

            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    # Step 4 — bulk download request
                    zip_name = create_bulk_download(token, region, id_chunk)

                    # Step 5 — wait for DONE
                    if not wait_for_zip(token, region, zip_name):
                        logger.error(f"Chunk {chunk_num} zip not ready, skipping")
                        total_failed += len(id_chunk)
                        continue

                    # Step 6 — download zip
                    zip_path = str(Path(tmpdir) / zip_name)
                    download_zip(token, region, zip_name, zip_path)

                    # Extract and upload each file
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        members = zf.namelist()
                        logger.info(f"Zip contains {len(members)} file(s)")

                        for member in members:
                            local_path = str(Path(tmpdir) / member)
                            zf.extract(member, tmpdir)

                            # Find matching object id by objectName
                            obj_id = next(
                                (oid for oid, name in id_to_name.items()
                                 if name == member or name.endswith(member)),
                                None
                            )

                            try:
                                upload_to_drive(
                                    drive_service, local_path,
                                    member, drive_folder_id
                                )
                                save_to_uploaded_log(
                                    obj_id if obj_id else f"{folder_name}/{member}"
                                )
                                total_uploaded += 1
                            except Exception as e:
                                logger.error(f"Upload failed '{member}': {e}")
                                total_failed += 1

                except requests.HTTPError as e:
                    logger.error(
                        f"Chunk {chunk_num} HTTP error: "
                        f"{e.response.status_code} {e.response.text[:300]}"
                    )
                    total_failed += len(id_chunk)
                except Exception as e:
                    logger.error(f"Chunk {chunk_num} failed: {e}")
                    total_failed += len(id_chunk)

        logger.info(
            f"Region {region} complete — "
            f"uploaded: {total_uploaded}, failed: {total_failed}"
        )

    logger.info(f"=== Sync completed for {folder_name} ===")


if __name__ == "__main__":
    run()