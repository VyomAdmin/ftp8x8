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
BULK_CHUNK_SIZE             = 50

TOKEN_URL = "https://api.8x8.com/oauth/v2/token"
BASE_URL  = "https://api.8x8.com/storage"
API_VER   = "v3"


# ── AUTH ──────────────────────────────────────────────────────────────────────
def get_access_token():
    logger.info("=== STEP 1: Getting access token ===")
    r = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30
    )
    logger.info(f"Token response status: {r.status_code}")
    r.raise_for_status()
    token = r.json()["access_token"]
    logger.info(f"Token obtained (first 20 chars): {token[:20]}...")
    return token


def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


# ── 8x8 API ───────────────────────────────────────────────────────────────────
def get_regions(token):
    logger.info("=== STEP 2: Getting regions ===")
    url = f"{BASE_URL}/us-east/{API_VER}/regions"
    logger.info(f"Regions URL: {url}")
    r = requests.get(url, headers=auth_headers(token), timeout=30)
    logger.info(f"Regions response status: {r.status_code}")
    logger.info(f"Regions response body: {r.text}")
    r.raise_for_status()
    return r.json()


def find_objects_for_yesterday(token, region, date_from, date_to):
    logger.info(f"=== STEP 3: Finding objects in region={region} ===")
    logger.info(f"Date range: {date_from} to {date_to}")

    all_objects = []
    page_key    = 0
    limit       = 100
    from_dt     = datetime.strptime(date_from, "%Y-%m-%dT%H:%M:%S")
    to_dt       = datetime.strptime(date_to,   "%Y-%m-%dT%H:%M:%S")

    while True:
        # NOTE: type filter only — date filter returns 500 on this account
        url = (
            f"{BASE_URL}/{region}/{API_VER}/objects"
            f"?filter=type==callcenterrecording"
            f"&sortField=createdTime&sortDirection=DESC"
            f"&pageKey={page_key}&limit={limit}"
        )
        logger.info(f"Fetching: {url}")

        try:
            r = requests.get(url, headers=auth_headers(token), timeout=60)
            logger.info(f"Response status: {r.status_code}")

            if r.status_code != 200:
                logger.error(f"API error {r.status_code}: {r.text[:500]}")
                break

            data       = r.json()
            content    = data.get("content", [])
            stop_early = False
            matched    = 0

            logger.info(
                f"Page {page_key}: {len(content)} records, "
                f"lastPage={data.get('lastPage')}"
            )

            for obj in content:
                created  = obj.get("createdTime", "")
                obj_id   = obj.get("id", "")
                obj_name = obj.get("objectName", "")
                try:
                    obj_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")
                    if from_dt <= obj_dt <= to_dt:
                        all_objects.append(obj)
                        matched += 1
                        logger.info(f"  MATCH: {created} | {obj_id} | {obj_name}")
                    elif obj_dt < from_dt:
                        logger.info(f"  STOP: record older than range: {created}")
                        stop_early = True
                        break
                except Exception as e:
                    logger.warning(f"  Date parse error for '{created}': {e}")

            logger.info(
                f"Page {page_key}: matched={matched}, "
                f"running total={len(all_objects)}"
            )

            if stop_early or data.get("lastPage", True):
                logger.info(
                    f"Stopping: stop_early={stop_early}, "
                    f"lastPage={data.get('lastPage')}"
                )
                break

            page_key = data.get("pageKey", page_key + limit)
            logger.info(f"Next page: {page_key}")

        except Exception as e:
            logger.error(f"Exception on page {page_key}: {e}", exc_info=True)
            break

    logger.info(f"Total matched objects: {len(all_objects)}")
    return all_objects


def create_bulk_download(token, region, object_ids):
    logger.info(f"=== STEP 4: Creating bulk download ({len(object_ids)} files) ===")
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/start"
    logger.info(f"POST {url}")
    logger.info(f"Payload IDs: {json.dumps(object_ids)}")
    r = requests.post(url, headers=auth_headers(token), json=object_ids, timeout=60)
    logger.info(f"Response status: {r.status_code}")
    logger.info(f"Response body: {r.text}")
    r.raise_for_status()
    zip_name = r.json()["zipName"]
    logger.info(f"zipName: {zip_name}")
    return zip_name


def wait_for_zip(token, region, zip_name, poll_secs=30, max_attempts=60):
    """
    Poll every 30s for up to 30 minutes.
    NOT_STARTED — 8x8 has queued the job, keep waiting.
    IN_PROGRESS — zip is being built.
    DONE        — ready to download.
    """
    logger.info(f"=== STEP 5: Polling zip status for {zip_name} ===")
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/status/{zip_name}"
    logger.info(f"Status URL: {url}")

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, headers=auth_headers(token), timeout=30)
            logger.info(
                f"Attempt {attempt}/{max_attempts}: "
                f"HTTP {r.status_code} | {r.text}"
            )
            r.raise_for_status()

            status = r.json().get("status", "UNKNOWN")

            if status == "DONE":
                logger.info("Zip is DONE — ready to download!")
                return True

            if status in ("ERROR", "FAILED"):
                logger.error(f"Zip failed with status: {status}")
                return False

            if status == "NOT_STARTED":
                logger.info(
                    f"NOT_STARTED — 8x8 has queued the job. "
                    f"Waiting {poll_secs}s..."
                )
            elif status == "IN_PROGRESS":
                logger.info(f"IN_PROGRESS — zip building. Waiting {poll_secs}s...")
            else:
                logger.info(f"Status={status} — waiting {poll_secs}s...")

            time.sleep(poll_secs)

        except Exception as e:
            logger.warning(f"Poll error on attempt {attempt}: {e}")
            time.sleep(poll_secs)

    logger.error(
        f"Timed out after {max_attempts * poll_secs // 60} minutes"
    )
    return False


def download_zip(token, region, zip_name, local_path):
    logger.info(f"=== STEP 6: Downloading zip {zip_name} ===")
    url = f"{BASE_URL}/{region}/{API_VER}/bulk/download/{zip_name}"
    logger.info(f"GET {url}")
    r = requests.get(
        url, headers=auth_headers(token), stream=True, timeout=300
    )
    logger.info(f"Response status: {r.status_code}")
    logger.info(f"Response headers: {dict(r.headers)}")
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    size_mb = Path(local_path).stat().st_size / (1024 * 1024)
    logger.info(f"Zip saved: {size_mb:.1f} MB → {local_path}")


# ── GOOGLE DRIVE ──────────────────────────────────────────────────────────────
def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive"]
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
    results = service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    existing = results.get("files", [])
    if existing:
        logger.info(
            f"Drive folder '{folder_name}' exists — id: {existing[0]['id']}"
        )
        return existing[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True
    ).execute()
    logger.info(f"Created folder '{folder_name}' — id: {folder['id']}")
    return folder["id"]


def upload_to_drive(service, local_path, filename, folder_id):
    logger.info(f"Uploading '{filename}'...")
    file_metadata = {"name": filename, "parents": [folder_id]}
    media         = MediaFileUpload(local_path, resumable=True)
    uploaded      = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name",
        supportsAllDrives=True
    ).execute()
    logger.info(f"Uploaded '{filename}' → Drive id: {uploaded.get('id')}")


def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║   8x8 → Google Drive sync started        ║")
    logger.info("╚══════════════════════════════════════════╝")

    yesterday   = datetime.utcnow() - timedelta(days=1)
    date_from   = yesterday.strftime("%Y-%m-%dT00:00:00")
    date_to     = yesterday.strftime("%Y-%m-%dT23:59:59")
    folder_name = yesterday.strftime("S%Y%m%d")

    logger.info(f"Target date    : {folder_name}")
    logger.info(f"Date from      : {date_from}")
    logger.info(f"Date to        : {date_to}")
    logger.info(f"Drive folder   : {GDRIVE_FOLDER_ID}")
    logger.info(f"Chunk size     : {BULK_CHUNK_SIZE}")
    logger.info(f"Uploaded log   : {UPLOADED_LOG}")

    already_uploaded = load_uploaded_log()
    logger.info(f"Already uploaded: {len(already_uploaded)} objects")

    drive_service   = get_drive_service()
    drive_folder_id = get_or_create_drive_folder(
        drive_service, folder_name, GDRIVE_FOLDER_ID
    )

    token   = get_access_token()
    regions = get_regions(token)

    for region in regions:
        logger.info(f"══ Region: {region} ══")

        objects = find_objects_for_yesterday(
            token, region, date_from, date_to
        )

        if not objects:
            logger.info(f"No objects for {folder_name} in {region}")
            continue

        new_objects = [o for o in objects if o["id"] not in already_uploaded]
        logger.info(
            f"Total={len(objects)} | "
            f"Already done={len(objects) - len(new_objects)} | "
            f"New={len(new_objects)}"
        )

        if not new_objects:
            logger.info("All already uploaded, skipping region")
            continue

        id_to_name = {
            o["id"]: o.get("objectName", f"{o['id']}.mp3")
            for o in new_objects
        }

        chunks = list(chunk_list(list(id_to_name.keys()), BULK_CHUNK_SIZE))
        logger.info(
            f"Processing {len(new_objects)} files "
            f"in {len(chunks)} chunk(s) of {BULK_CHUNK_SIZE}"
        )

        total_uploaded = 0
        total_failed   = 0

        for chunk_num, id_chunk in enumerate(chunks, 1):
            logger.info(
                f"── Chunk {chunk_num}/{len(chunks)}: "
                f"{len(id_chunk)} files ──"
            )
            logger.info(f"IDs: {id_chunk}")

            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    # Step 4 — request bulk zip
                    zip_name = create_bulk_download(token, region, id_chunk)

                    # Step 5 — wait for DONE
                    if not wait_for_zip(token, region, zip_name):
                        logger.error(
                            f"Chunk {chunk_num} zip not ready, skipping"
                        )
                        total_failed += len(id_chunk)
                        continue

                    # Step 6 — download zip
                    zip_path = str(Path(tmpdir) / zip_name)
                    download_zip(token, region, zip_name, zip_path)

                    # Extract and upload each file
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        members = zf.namelist()
                        logger.info(
                            f"Zip contains {len(members)} file(s): {members}"
                        )

                        for member in members:
                            local_path = str(Path(tmpdir) / member)
                            zf.extract(member, tmpdir)

                            # Match filename back to object id
                            obj_id = next(
                                (oid for oid, name in id_to_name.items()
                                 if name == member or
                                 name.endswith(member) or
                                 member.endswith(name)),
                                None
                            )
                            logger.info(
                                f"member='{member}' → obj_id={obj_id}"
                            )

                            try:
                                upload_to_drive(
                                    drive_service,
                                    local_path,
                                    member,
                                    drive_folder_id
                                )
                                save_to_uploaded_log(
                                    obj_id if obj_id
                                    else f"{folder_name}/{member}"
                                )
                                total_uploaded += 1

                            except Exception as e:
                                logger.error(
                                    f"Upload failed '{member}': {e}",
                                    exc_info=True
                                )
                                total_failed += 1

                except requests.HTTPError as e:
                    logger.error(
                        f"HTTP {e.response.status_code}: "
                        f"{e.response.text[:500]}"
                    )
                    total_failed += len(id_chunk)

                except Exception as e:
                    logger.error(
                        f"Chunk {chunk_num} error: {e}",
                        exc_info=True
                    )
                    total_failed += len(id_chunk)

        logger.info(
            f"Region {region} complete — "
            f"uploaded={total_uploaded}, failed={total_failed}"
        )

    logger.info(f"=== Sync completed for {folder_name} ===")


if __name__ == "__main__":
    run()