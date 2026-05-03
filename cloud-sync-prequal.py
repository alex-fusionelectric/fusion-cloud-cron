"""Cloud-side Prequal scanner. Replaces the local Convert-Prequal.ps1
pipeline so the Bay Bid List page's prequal pills update without Alex's
PC being on.

Flow:
  1. Read active EST#s from public.bids_cloud (status in BIDDING / BID OR BAIL).
  2. List the Dropbox 02- ESTIMATING folder, find one EST# subfolder per active bid.
  3. Walk PLANS & SPECS for spec-PDF candidates (same heuristics as local scan-prequal.py).
  4. For each candidate, look up cached extracted text in public.dropbox_kv_cloud
     keyed by path_lower + '@' + rev. Miss => download bytes, extract via pypdf, upsert.
  5. Run scan_prequal_compat.search_pdf() on the cached text -> verdict + evidence.
  6. Hard-coded SHC etc. rules win over PDF scanning (matches local behavior).
  7. Upsert single 'current' row to public.prequal_cloud.

Required env vars:
  SUPABASE_SERVICE_KEY     -- Supabase service-role key (RLS bypass)
  DROPBOX_REFRESH_TOKEN    -- long-lived OAuth refresh token
  DROPBOX_APP_KEY          -- Dropbox app key (public)
  DROPBOX_APP_SECRET       -- Dropbox app secret (private)

Optional env vars:
  DROPBOX_ROOT             -- root path inside the Dropbox account, default "/FUSION ELECTRIC Folder/02- ESTIMATING"
  PREQUAL_BID_LIMIT        -- cap to N bids for testing
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import dropbox  # noqa: E402
from dropbox import common as dropbox_common  # noqa: E402
from dropbox.exceptions import ApiError, AuthError  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import scan_prequal_compat as sp  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
PREQUAL_TABLE = "prequal_cloud"
KV_TABLE = "dropbox_kv_cloud"
BIDS_TABLE = "bids_cloud"

DEFAULT_DROPBOX_ROOT = "/Fusion Electric Folder/02- ESTIMATING"
ACTIVE_BID_STATUSES = {"BIDDING", "BID OR BAIL"}

# Per-bid candidate cap. Local scanner does up to ~5 candidates per bid;
# cloud honors the same shape but skips files >5 MB to keep download budget reasonable.
MAX_CANDIDATES_PER_BID = 5
MAX_PDF_BYTES = 5 * 1024 * 1024  # 5 MB


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
    # Server-side filter -- bids_cloud has 1800+ rows and PostgREST
    # default-limits to 1000 in PK order, which would silently drop the
    # active bids (they live near the end of the est_number ordering).
    # Spaces inside quoted in.() values need URL-encoding.
    in_value = urllib.parse.quote('("BIDDING","BID OR BAIL")', safe='()",')
    qs = f"status=in.{in_value}&select=est_number,status,project_name&order=est_number"
    status, body = _sb_request("GET", f"{BIDS_TABLE}?{qs}")
    if status != 200:
        raise SystemExit(f"bids_cloud GET failed: HTTP {status} {body[:200]!r}")
    rows = json.loads(body)
    out = []
    for r in rows:
        est = (r.get("est_number") or "").strip().upper()
        if est:
            out.append({"est": est, "project_name": r.get("project_name") or ""})
    print(f"Active bids from bids_cloud: {len(out)} (filter: {sorted(ACTIVE_BID_STATUSES)})")
    return out


def kv_get_many(keys):
    """Bulk SELECT from dropbox_kv_cloud. Returns {key: value-jsonb}."""
    if not keys:
        return {}
    out = {}
    BATCH = 50  # PostgREST URL length cap
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        in_clause = ",".join(f'"{urllib.parse.quote(k, safe="")}"' for k in chunk)
        path = f"{KV_TABLE}?key=in.({in_clause})&select=key,value"
        status, body = _sb_request("GET", path)
        if status != 200:
            print(f"  [warn] kv_get_many HTTP {status}: {body[:200]!r}")
            continue
        for r in json.loads(body):
            out[r["key"]] = r["value"]
    return out


def kv_upsert(key, value):
    body = [{"key": key, "value": value, "updated_at": datetime.utcnow().isoformat() + "Z"}]
    status, resp = _sb_request(
        "POST", KV_TABLE, body=body,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=30,
    )
    if status not in (200, 201, 204):
        print(f"  [warn] kv_upsert HTTP {status}: {resp[:200]!r}")


def upsert_prequal_row(payload):
    body = [{
        "id": "current",
        "payload": payload,
        "generated_at": payload.get("generated_at"),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }]
    status, resp = _sb_request(
        "POST", PREQUAL_TABLE, body=body,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=30,
    )
    if status not in (200, 201, 204):
        raise SystemExit(f"prequal_cloud upsert failed: HTTP {status} {resp[:200]!r}")
    print(f"Upserted prequal_cloud row 'current'.")


# --- Dropbox helpers ---------------------------------------------------------

def dropbox_client():
    """Return a Dropbox client configured to read the TEAM root namespace
    when the authenticating user is part of a Dropbox Business team. The
    Fusion Electric folders live in the team's root namespace (not the
    user's personal namespace), so without Dropbox-API-Path-Root the
    list_folder calls 404 even though the local Dropbox client shows
    them. Also works for plain personal accounts (root == home)."""
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
        acct = dbx.users_get_current_account()
    except AuthError as e:
        raise SystemExit(f"Dropbox auth failed: {e}")
    ri = acct.root_info
    root_ns = getattr(ri, "root_namespace_id", None)
    home_ns = getattr(ri, "home_namespace_id", None)
    print(f"Dropbox account: {acct.email} (team_ns={root_ns}, home_ns={home_ns})")
    if root_ns and root_ns != home_ns:
        dbx = dbx.with_path_root(dropbox_common.PathRoot.root(root_ns))
        print(f"  Using team root namespace {root_ns}")
    return dbx


def list_folder_recursive(dbx, path, *, max_entries=2000):
    """Yield FileMetadata + FolderMetadata under path. Caps at max_entries."""
    entries = []
    res = dbx.files_list_folder(path, recursive=True, include_non_downloadable_files=False)
    entries.extend(res.entries)
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
    """Find the EST# subfolder for `est_number` under root_path. Matches
    folder names like 'EST# 26-203 ...' (case-insensitive)."""
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


def pick_candidates(folder_entries, *, plans_root_lower):
    """Mirror of scan-prequal.candidate_specs() but operating on a
    flat list of Dropbox FileMetadata. Returns up to MAX_CANDIDATES_PER_BID
    in priority order (P1 spec-name hits, then P2 spec-folder hits)."""
    p1, p2, p3 = [], [], []
    for f in folder_entries:
        if not isinstance(f, dropbox.files.FileMetadata):
            continue
        if not f.name.lower().endswith(".pdf"):
            continue
        if f.size and f.size > MAX_PDF_BYTES:
            continue
        path_lc = f.path_lower
        # Strip the bid root prefix so SKIP_FOLDER_HINTS substring tests
        # don't collide with the parent path -- e.g. an "ARCHIVE" team
        # folder higher up would otherwise nuke every bid.
        rel_lc = path_lc[len(plans_root_lower):] if path_lc.startswith(plans_root_lower) else path_lc
        if any(skip in rel_lc for skip in sp.SKIP_FOLDER_HINTS):
            continue
        name_lc = f.name.lower()
        if any(h in name_lc for h in sp.SPEC_FILE_HINTS):
            p1.append(f)
        elif any(h in rel_lc for h in sp.SPEC_FOLDER_HINTS):
            p2.append(f)
        else:
            p3.append(f)
    out = []
    out.extend(p1[:3])
    out.extend(p2[:2])
    if len(out) < MAX_CANDIDATES_PER_BID:
        out.extend(p3[:MAX_CANDIDATES_PER_BID - len(out)])
    return out[:MAX_CANDIDATES_PER_BID]


def cache_key_for(file_meta):
    return f"{file_meta.path_lower}@{file_meta.rev}"


def get_or_extract_text(dbx, file_meta, kv_cache_local):
    """Return (text, was_cached). Prefer local-batch cache, then
    dropbox_kv_cloud, then download + pypdf + upsert."""
    key = cache_key_for(file_meta)
    cached = kv_cache_local.get(key)
    if cached and isinstance(cached, dict) and "text" in cached:
        return cached["text"], True
    # Cache miss: download + extract.
    try:
        _, resp = dbx.files_download(file_meta.path_lower)
        pdf_bytes = resp.content
    except (ApiError, urllib.error.HTTPError) as e:
        print(f"  [warn] download failed for {file_meta.path_display}: {e}")
        return "", False
    text, err = sp.extract_text_from_bytes(pdf_bytes, max_pages=40)
    if err:
        print(f"  [warn] pypdf failed for {file_meta.name}: {err}")
        text = ""
    # Cap stored size to keep KV table compact.
    stored = text[:60_000]
    kv_upsert(key, {"text": stored, "name": file_meta.name, "size": file_meta.size})
    return stored, False


# --- Main loop ---------------------------------------------------------------

def main():
    root_path = (os.environ.get("DROPBOX_ROOT") or DEFAULT_DROPBOX_ROOT).rstrip("/")
    bid_limit = int(os.environ.get("PREQUAL_BID_LIMIT") or 0)
    print(f"Dropbox root: {root_path}")

    dbx = dropbox_client()
    print("Dropbox auth OK.")

    bids = fetch_active_bids()
    if bid_limit:
        bids = bids[:bid_limit]
        print(f"  (capped to first {bid_limit} for testing)")

    # Resolve EST# -> dropbox folder.
    bid_folders = []  # [(est, dropbox_folder_path_lower, folder_name)]
    for b in bids:
        match = find_bid_folder_path(dbx, root_path, b["est"])
        if match:
            path_lower, folder_name = match
            # Hard-coded rules (e.g. SHC) bypass the PDF scan entirely. Detect
            # using the Dropbox folder display name rather than the bids_cloud
            # 'name' column so we match what the local scanner sees.
            bid_folders.append({"est": b["est"], "path": path_lower, "folder_name": folder_name})
        else:
            bid_folders.append({"est": b["est"], "path": None, "folder_name": b["project_name"]})

    # First pass: collect every file-metadata we'll consider so we can
    # batch-load the kv cache (one Supabase round-trip instead of N).
    listings = {}  # est -> [FileMetadata, ...]
    candidates_by_est = {}
    for bf in bid_folders:
        if bf["path"] is None:
            continue
        if sp.hardcoded_prequal_for(bf["folder_name"]):
            continue  # rule short-circuits PDF work
        try:
            entries = list_folder_recursive(dbx, bf["path"], max_entries=400)
        except ApiError as e:
            print(f"  [warn] list failed for {bf['path']}: {e}")
            continue
        listings[bf["est"]] = entries
        candidates_by_est[bf["est"]] = pick_candidates(entries, plans_root_lower=bf["path"] + "/")

    all_keys = []
    for cands in candidates_by_est.values():
        for f in cands:
            all_keys.append(cache_key_for(f))
    print(f"Looking up {len(all_keys)} cache key(s) in {KV_TABLE}...")
    kv_cache = kv_get_many(all_keys)
    print(f"  cache hits: {len(kv_cache)}")

    # Second pass: classify.
    out = {}
    started = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    fetched_count = 0
    for bf in bid_folders:
        est = bf["est"]
        rule = sp.hardcoded_prequal_for(bf["folder_name"])
        if rule:
            out[est] = {
                "prequal_required": rule["verdict"],
                "evidence": rule["evidence"],
                "source_file": "",
                "scanned_pdfs": 0,
                "fetched_from_cloud": 0,
                "scanned_at": started,
                "rule_applied": rule["match"],
            }
            print(f"  {est:>8}  {rule['verdict']:>7}  (rule: {rule['match']})")
            continue
        if bf["path"] is None:
            out[est] = {
                "prequal_required": "unknown",
                "evidence": "",
                "source_file": "",
                "scanned_pdfs": 0,
                "fetched_from_cloud": 0,
                "scanned_at": started,
                "note": "no Dropbox folder match",
            }
            print(f"  {est:>8}  unknown  (no Dropbox folder)")
            continue
        candidates = candidates_by_est.get(est, [])
        verdict, evidence, source_file = "unknown", "", ""
        scanned, fetched = 0, 0
        for f in candidates:
            text, was_cached = get_or_extract_text(dbx, f, kv_cache)
            scanned += 1
            if not was_cached:
                fetched += 1
                fetched_count += 1
            if not text:
                continue
            v, e = sp.search_pdf(text)
            if v == "yes":
                verdict, evidence = "yes", e.strip()
                source_file = f.path_display.split(bf["folder_name"], 1)[-1].lstrip("/")
                break
            if v == "no" and verdict != "yes":
                verdict, evidence = "no", e.strip()
                source_file = f.path_display.split(bf["folder_name"], 1)[-1].lstrip("/")
        out[est] = {
            "prequal_required": verdict,
            "evidence": evidence[:280],
            "source_file": source_file,
            "scanned_pdfs": scanned,
            "fetched_from_cloud": fetched,
            "scanned_at": started,
        }
        fetch_note = f" +{fetched} fetched" if fetched else ""
        print(f"  {est:>8}  {verdict:>7}  ({scanned} pdfs scanned{fetch_note})")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bids": out,
    }
    yes = sum(1 for v in out.values() if v["prequal_required"] == "yes")
    no = sum(1 for v in out.values() if v["prequal_required"] == "no")
    unk = sum(1 for v in out.values() if v["prequal_required"] == "unknown")
    print(f"\nSummary: {yes} yes / {no} no / {unk} unknown — total Dropbox downloads this run: {fetched_count}")

    upsert_prequal_row(payload)


if __name__ == "__main__":
    main()
