"""reverse-auto-filed-quotes.py -- One-shot UNDO of the auto-file-vendor-quotes
cron's runs. Lists the live QUOTES folder for each target bid via the
Dropbox API, deletes only files matching the cron's
'<Vendor> - YYYY-MM-DD - <original>' naming pattern, and leaves anything
else (manually-placed quotes) untouched.

Also clears matching rows from quote_files_cloud if any are still there.

Required env (same secrets the original cron uses):
  SUPABASE_SERVICE_KEY
  GMAIL_TOKEN_JSON          (only used for AUTO-FILED label cleanup)
  DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET

Optional env:
  EST_NUMBER_FILTER  -- only act on this EST# (e.g. '26-243'). Empty = all.
  DRY_RUN            -- '1' to preview without making changes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    import dropbox  # type: ignore
    from dropbox import common as dropbox_common  # type: ignore
    from dropbox.exceptions import AuthError  # type: ignore
except ImportError as exc:
    print(f"[error] dropbox lib missing: {exc}", file=sys.stderr)
    sys.exit(2)


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
DRY_RUN      = (os.environ.get("DRY_RUN") or "").strip() == "1"
EST_FILTER   = (os.environ.get("EST_NUMBER_FILTER") or "").strip()

# Cron filenames are '<Vendor> - YYYY-MM-DD - <original>' per
# auto-file-vendor-quotes.py:filed_filename(). Anything matching this
# pattern in a QUOTES folder is something the cron put there.
CRON_FILE_RX = re.compile(r"^.+? - \d{4}-\d{2}-\d{2} - .+", re.IGNORECASE)


# --- Supabase --------------------------------------------------------------

def _sb_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required")
    return k


def _sb(method: str, path: str, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": _sb_key(), "Authorization": f"Bearer {_sb_key()}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8")


# --- Dropbox (matches auto-file-vendor-quotes.py exactly) -----------------

def dropbox_client():
    rt = (os.environ.get("DROPBOX_REFRESH_TOKEN") or "").strip()
    ak = (os.environ.get("DROPBOX_APP_KEY") or "").strip()
    asec = (os.environ.get("DROPBOX_APP_SECRET") or "").strip()
    if not (rt and ak and asec):
        raise SystemExit("DROPBOX_REFRESH_TOKEN/APP_KEY/APP_SECRET all required")
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=rt, app_key=ak, app_secret=asec, timeout=60,
    )
    try:
        acct = dbx.users_get_current_account()
    except AuthError as e:
        raise SystemExit(f"Dropbox auth failed: {e}")
    ri = acct.root_info
    root_ns = getattr(ri, "root_namespace_id", None)
    home_ns = getattr(ri, "home_namespace_id", None)
    if root_ns and root_ns != home_ns:
        dbx = dbx.with_path_root(dropbox_common.PathRoot.root(root_ns))
        print(f"  [info] using Dropbox team root namespace {root_ns}")
    return dbx


def list_quotes_folder(dbx, quotes_path: str) -> list[dict]:
    """Return [{name, path_lower, path_display}] for every FILE in the folder.
    Empty list if the folder doesn't exist."""
    try:
        res = dbx.files_list_folder(quotes_path, recursive=False, limit=2000)
    except dropbox.exceptions.ApiError as e:
        if "not_found" in str(e).lower():
            return []
        raise
    out = []
    for entry in res.entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            out.append({
                "name": entry.name,
                "path_lower": entry.path_lower,
                "path_display": entry.path_display,
                "size": entry.size,
            })
    return out


def delete_dropbox_path(dbx, path: str) -> tuple[bool, str]:
    if DRY_RUN:
        return True, "dry-run"
    try:
        dbx.files_delete_v2(path)
        return True, "ok"
    except dropbox.exceptions.ApiError as e:
        msg = str(e)
        if "not_found" in msg.lower():
            return True, "already gone"
        return False, msg[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


# --- Gmail (for AUTO-FILED label cleanup, best-effort) --------------------

def gmail_service_or_none():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid:
            return None
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"  [info] gmail not initialized: {e}")
        return None


def find_label_id(svc, name: str) -> str | None:
    if not svc: return None
    try:
        labels = svc.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in labels:
            if lbl.get("name") == name:
                return lbl.get("id")
    except Exception:
        pass
    return None


# --- Active bids (read from prebid_bids_cloud + bid_setup_completions) ----

def fetch_target_bids() -> list[dict]:
    """Return [{est_number, dropbox_folder, gmail_label}] for the bids we
    want to clean -- filtered to EST_FILTER when set, otherwise every bid
    that was ever touched by the auto-filer (anything with a QUOTES
    folder under it)."""
    if EST_FILTER:
        st, body = _sb("GET",
            f"prebid_bids_cloud?est_number=eq.{urllib.parse.quote(EST_FILTER)}"
            f"&select=id,est_number,dropbox_folder,gmail_label"
            f"&order=created_at.desc&limit=10")
    else:
        # Pull every prebid bid that has a dropbox folder set. Bigger blast
        # radius -- only run unfiltered when you mean it.
        st, body = _sb("GET",
            "prebid_bids_cloud?dropbox_folder=not.is.null"
            "&select=id,est_number,dropbox_folder,gmail_label&limit=500")
    if st != 200:
        raise SystemExit(f"prebid_bids_cloud GET failed: HTTP {st}: {body[:200]}")
    rows = json.loads(body)
    # Dedup by est_number, prefer the row with a non-null dropbox_folder
    seen: dict = {}
    for r in rows:
        est = r.get("est_number")
        if not est: continue
        if est not in seen or (r.get("dropbox_folder") and not seen[est].get("dropbox_folder")):
            seen[est] = r
    return list(seen.values())


# --- Main ------------------------------------------------------------------

def main():
    print(f"=== reverse-auto-filed-quotes starting "
          f"(DRY_RUN={DRY_RUN}, EST_FILTER={EST_FILTER or '(all touched)'}) ===")

    bids = fetch_target_bids()
    print(f"  {len(bids)} bid(s) in scope")
    if not bids:
        print("  nothing to do.")
        return

    dbx = dropbox_client()
    svc = gmail_service_or_none()
    auto_label_id = find_label_id(svc, "AUTO-FILED") if svc else None
    if auto_label_id:
        print(f"  AUTO-FILED label id: {auto_label_id}")

    deleted_files = 0
    kept_manual   = 0
    skipped       = 0

    for bid in bids:
        est = bid.get("est_number") or "?"
        dbx_folder = (bid.get("dropbox_folder") or "").rstrip("/")
        if not dbx_folder:
            print(f"  [skip] EST# {est}: no dropbox_folder set")
            continue
        quotes_path = dbx_folder + "/QUOTES"
        print(f"\n  EST# {est}: scanning {quotes_path}")

        try:
            files = list_quotes_folder(dbx, quotes_path)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] list failed: {e}")
            continue

        if not files:
            print(f"  [info] empty / missing folder, nothing to clean")
            continue
        print(f"  found {len(files)} file(s) in QUOTES")

        for f in files:
            name = f["name"]
            if CRON_FILE_RX.match(name):
                ok, detail = delete_dropbox_path(dbx, f["path_display"])
                if ok:
                    deleted_files += 1
                    print(f"    [del] {name}  ({detail})")
                else:
                    skipped += 1
                    print(f"    [skip-err] {name}  ({detail})")
            else:
                kept_manual += 1
                print(f"    [keep] {name}  (manual, no Vendor-Date prefix)")

    # Clean up any matching audit rows still in quote_files_cloud
    rows_cleared = 0
    if not DRY_RUN and EST_FILTER:
        try:
            _sb("DELETE",
                f"quote_files_cloud?est_number=eq.{urllib.parse.quote(EST_FILTER)}")
            rows_cleared = "all-for-EST"
        except Exception as e:
            print(f"  [warn] quote_files_cloud cleanup failed: {e}")

    print(f"\n=== done ===")
    print(f"  deleted from Dropbox:  {deleted_files}")
    print(f"  kept (manual files):   {kept_manual}")
    print(f"  delete errors:         {skipped}")
    print(f"  quote_files_cloud:     {rows_cleared}")
    if DRY_RUN:
        print("\n  DRY_RUN was set -- nothing was actually changed.")


if __name__ == "__main__":
    main()
