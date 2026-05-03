"""Cloud-side Divisions scanner. Replaces local Convert-Divisions.ps1.

Scans active-bid spec PDFs in Dropbox for CSI section numbers (Div 26/27/28),
maps to Fusion scopes, writes payload to public.divisions_cloud.

Shares the dropbox_kv_cloud cache with cloud-sync-prequal.py -- key format
is identical (`{path_lower}@{rev}`) so a PDF extracted by one script is a
cache hit for the other.

Required env vars:
  SUPABASE_SERVICE_KEY     -- Supabase service-role key
  DROPBOX_REFRESH_TOKEN    -- long-lived OAuth refresh token
  DROPBOX_APP_KEY          -- Dropbox app key
  DROPBOX_APP_SECRET       -- Dropbox app secret

Optional:
  DROPBOX_ROOT             -- default "/FUSION ELECTRIC Folder/02- ESTIMATING"
  DIVISIONS_BID_LIMIT      -- cap to N bids for testing
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import dropbox  # noqa: E402
from dropbox.exceptions import ApiError, AuthError  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import scan_divisions_compat as sd  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
DIVISIONS_TABLE = "divisions_cloud"
KV_TABLE = "dropbox_kv_cloud"
BIDS_TABLE = "bids_cloud"

DEFAULT_DROPBOX_ROOT = "/FUSION ELECTRIC Folder/02- ESTIMATING"
ACTIVE_BID_STATUSES = {"BIDDING", "BID OR BAIL"}

# Spec-name keywords used to pick PDF candidates -- mirrors local
# scan-divisions.candidate_specs(). Looser than prequal's spec heuristic
# because section numbers can appear in addenda + plan-set TOCs too.
SPEC_NAME_HINTS = [
    "spec", "specs", "specifications", "manual",
    "table of contents", "toc",
    "26 05", "26 00", "27 00", "28 00",
    "div 26", "div 27", "div 28",
]
MAX_CANDIDATES_PER_BID = 6
MAX_PDF_BYTES = 5 * 1024 * 1024


# --- Supabase REST helpers ---------------------------------------------------

def _service_key():
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY env var is required.")
    return key


def _sb_request(method, path, *, body=None, headers_extra=None, timeout=30):
    key = _service_key()
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def fetch_active_bids():
    status, body = _sb_request("GET", f"{BIDS_TABLE}?select=est_number,status,name&order=est_number")
    if status != 200:
        raise SystemExit(f"bids_cloud GET failed: HTTP {status} {body[:200]!r}")
    rows = json.loads(body)
    out = []
    for r in rows:
        est = (r.get("est_number") or "").strip().upper()
        st = (r.get("status") or "").strip().upper()
        if est and st in ACTIVE_BID_STATUSES:
            out.append({"est": est, "name": r.get("name") or ""})
    print(f"Active bids from bids_cloud: {len(out)}")
    return out


def kv_get_many(keys):
    if not keys:
        return {}
    out = {}
    BATCH = 50
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        in_clause = ",".join(f'"{urllib.parse.quote(k, safe="")}"' for k in chunk)
        path = f"{KV_TABLE}?key=in.({in_clause})&select=key,value"
        status, body = _sb_request("GET", path)
        if status != 200:
            print(f"  [warn] kv_get_many HTTP {status}")
            continue
        for r in json.loads(body):
            out[r["key"]] = r["value"]
    return out


def kv_upsert(key, value):
    body = [{"key": key, "value": value, "updated_at": datetime.utcnow().isoformat() + "Z"}]
    status, resp = _sb_request(
        "POST", KV_TABLE, body=body,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if status not in (200, 201, 204):
        print(f"  [warn] kv_upsert HTTP {status}: {resp[:200]!r}")


def upsert_divisions_row(payload):
    body = [{
        "id": "current",
        "payload": payload,
        "generated_at": payload.get("generated_at"),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }]
    status, resp = _sb_request(
        "POST", DIVISIONS_TABLE, body=body,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if status not in (200, 201, 204):
        raise SystemExit(f"divisions_cloud upsert failed: HTTP {status} {resp[:200]!r}")
    print(f"Upserted divisions_cloud row 'current'.")


# --- Dropbox helpers (duplicated minimally from prequal — small enough
# to keep both scripts standalone without a shared module) -------------------

def dropbox_client():
    refresh = (os.environ.get("DROPBOX_REFRESH_TOKEN") or "").strip()
    app_key = (os.environ.get("DROPBOX_APP_KEY") or "").strip()
    app_secret = (os.environ.get("DROPBOX_APP_SECRET") or "").strip()
    if not (refresh and app_key and app_secret):
        raise SystemExit("DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET all required.")
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=refresh,
        app_key=app_key,
        app_secret=app_secret,
        timeout=60,
    )
    try:
        dbx.users_get_current_account()
    except AuthError as e:
        raise SystemExit(f"Dropbox auth failed: {e}")
    return dbx


def list_folder_recursive(dbx, path, *, max_entries=2000):
    res = dbx.files_list_folder(path, recursive=True, include_non_downloadable_files=False)
    entries = list(res.entries)
    while res.has_more and len(entries) < max_entries:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)
    return entries[:max_entries]


def list_folder_shallow(dbx, path):
    res = dbx.files_list_folder(path)
    out = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        out.extend(res.entries)
    return out


def find_bid_folder_path(dbx, root_path, est_number):
    try:
        entries = list_folder_shallow(dbx, root_path)
    except ApiError as e:
        print(f"  [warn] could not list {root_path}: {e}")
        return None
    pat = re.compile(rf"^EST#\s*{re.escape(est_number)}\b", re.IGNORECASE)
    for e in entries:
        if isinstance(e, dropbox.files.FolderMetadata) and pat.match(e.name):
            return e.path_lower, e.name
    return None


def pick_candidates(folder_entries):
    """Mirror of scan-divisions.candidate_specs() over Dropbox metadata."""
    out = []
    for f in folder_entries:
        if not isinstance(f, dropbox.files.FileMetadata):
            continue
        name_lc = f.name.lower()
        if not name_lc.endswith(".pdf"):
            continue
        if f.size and f.size > MAX_PDF_BYTES:
            continue
        if any(h in name_lc for h in SPEC_NAME_HINTS):
            out.append(f)
        if len(out) >= MAX_CANDIDATES_PER_BID:
            break
    return out


def cache_key_for(file_meta):
    return f"{file_meta.path_lower}@{file_meta.rev}"


def get_or_extract_text(dbx, file_meta, kv_cache_local):
    key = cache_key_for(file_meta)
    cached = kv_cache_local.get(key)
    if cached and isinstance(cached, dict) and "text" in cached:
        return cached["text"], True
    try:
        _, resp = dbx.files_download(file_meta.path_lower)
        pdf_bytes = resp.content
    except (ApiError, urllib.error.HTTPError) as e:
        print(f"  [warn] download failed for {file_meta.path_display}: {e}")
        return "", False
    text = sd.extract_text_from_bytes(pdf_bytes)
    stored = text[:80_000]
    kv_upsert(key, {"text": stored, "name": file_meta.name, "size": file_meta.size})
    return stored, False


# --- Main --------------------------------------------------------------------

def main():
    root_path = (os.environ.get("DROPBOX_ROOT") or DEFAULT_DROPBOX_ROOT).rstrip("/")
    bid_limit = int(os.environ.get("DIVISIONS_BID_LIMIT") or 0)
    print(f"Dropbox root: {root_path}")

    dbx = dropbox_client()
    print("Dropbox auth OK.")

    bids = fetch_active_bids()
    if bid_limit:
        bids = bids[:bid_limit]
        print(f"  (capped to first {bid_limit} for testing)")

    # Resolve each EST# to its Dropbox folder, then collect candidates.
    bid_folders = []
    for b in bids:
        match = find_bid_folder_path(dbx, root_path, b["est"])
        if match:
            path_lower, folder_name = match
            bid_folders.append({"est": b["est"], "path": path_lower, "folder_name": folder_name})
        else:
            bid_folders.append({"est": b["est"], "path": None, "folder_name": b["name"]})

    candidates_by_est = {}
    for bf in bid_folders:
        if bf["path"] is None:
            continue
        try:
            entries = list_folder_recursive(dbx, bf["path"], max_entries=400)
        except ApiError as e:
            print(f"  [warn] list failed for {bf['path']}: {e}")
            continue
        candidates_by_est[bf["est"]] = pick_candidates(entries)

    all_keys = []
    for cands in candidates_by_est.values():
        for f in cands:
            all_keys.append(cache_key_for(f))
    print(f"Looking up {len(all_keys)} cache key(s) in {KV_TABLE}...")
    kv_cache = kv_get_many(all_keys)
    print(f"  cache hits: {len(kv_cache)}")

    out = {}
    started = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    fetched_count = 0
    for bf in bid_folders:
        est = bf["est"]
        if bf["path"] is None:
            out[est] = {
                "divisions": [],
                "sections": [],
                "recommended_scopes": [],
                "evidence": "",
                "scanned_pdfs": 0,
                "scanned_at": started,
                "note": "no Dropbox folder match",
            }
            print(f"  {est:>8}  divs=—       scopes=—  (no Dropbox folder)")
            continue
        candidates = candidates_by_est.get(est, [])
        scanned = 0
        all_sections = set()
        first_evidence = ""
        first_evidence_file = ""
        for f in candidates:
            text, was_cached = get_or_extract_text(dbx, f, kv_cache)
            scanned += 1
            if not was_cached:
                fetched_count += 1
            if not text:
                continue
            secs = sd.scan_pdf_for_sections(text)
            if secs:
                if not first_evidence:
                    first_evidence = f"Found {len(secs)} CSI sections in {f.name}"
                    first_evidence_file = f.name
                all_sections.update(secs)
        scopes = sd.map_sections_to_scopes(all_sections)
        divisions = sorted({s[:2] for s in all_sections if s[:2] in {"26", "27", "28"}})
        out[est] = {
            "divisions": divisions,
            "sections": sorted(all_sections),
            "recommended_scopes": scopes,
            "evidence": first_evidence,
            "scanned_pdfs": scanned,
            "scanned_at": started,
        }
        print(f"  {est:>8}  divs={','.join(divisions) or '—':<8} scopes={','.join(scopes) or '—'}")

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "bids": out}
    print(f"\nDropbox downloads this run: {fetched_count}")

    upsert_divisions_row(payload)


if __name__ == "__main__":
    main()
