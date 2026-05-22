"""reverse-auto-filed-quotes.py -- One-shot UNDO of the auto-file-vendor-quotes
cron's runs. Walks every row in quote_files_cloud, deletes the corresponding
Dropbox file, removes the AUTO-FILED Gmail label from the source thread,
and finally deletes the audit row. Restores both bids' QUOTES folders to
the state they were in before the cron started running.

Intentionally separate workflow + manual dispatch only -- not something we
want a misfire on.

Required env (same secrets the original cron uses):
  SUPABASE_SERVICE_KEY
  GMAIL_TOKEN_JSON
  DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET

Dry-run support: set DRY_RUN=1 to print the plan without deleting anything.
"""
from __future__ import annotations

import json
import os
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
except ImportError as exc:
    print(f"[error] dropbox lib missing: {exc}", file=sys.stderr)
    sys.exit(2)


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
TABLE        = "quote_files_cloud"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
DRY_RUN      = (os.environ.get("DRY_RUN") or "").strip() == "1"


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


# --- Dropbox ---------------------------------------------------------------

def dropbox_client():
    rt  = os.environ.get("DROPBOX_REFRESH_TOKEN") or ""
    ak  = os.environ.get("DROPBOX_APP_KEY")       or ""
    asec = os.environ.get("DROPBOX_APP_SECRET")    or ""
    if not (rt and ak and asec):
        raise SystemExit("DROPBOX_REFRESH_TOKEN / DROPBOX_APP_KEY / DROPBOX_APP_SECRET required")
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=rt, app_key=ak, app_secret=asec,
        timeout=30,
    )
    # The watcher operates on the team namespace; match the original cron.
    try:
        team_root = os.environ.get("DROPBOX_TEAM_ROOT_NAMESPACE_ID")
        if team_root:
            dbx = dbx.with_path_root(dropbox_common.PathRoot.root(team_root))
    except Exception as e:
        print(f"  [warn] could not set Dropbox path root: {e}")
    return dbx


def dropbox_delete(dbx, path: str) -> tuple[bool, str]:
    if DRY_RUN:
        return True, "dry-run"
    try:
        dbx.files_delete_v2(path)
        return True, "ok"
    except dropbox.exceptions.ApiError as e:
        # If the file is already gone, that's fine -- we want it gone.
        msg = str(e)
        if "not_found" in msg.lower():
            return True, "already gone"
        return False, msg[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


# --- Gmail -----------------------------------------------------------------

def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required")
    creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def find_label_id(svc, name: str) -> str | None:
    try:
        labels = svc.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in labels:
            if lbl.get("name") == name:
                return lbl.get("id")
    except Exception as e:
        print(f"  [warn] labels list failed: {e}")
    return None


def remove_label_from_thread(svc, thread_id: str, label_id: str) -> bool:
    if DRY_RUN:
        return True
    try:
        svc.users().threads().modify(
            userId="me", id=thread_id,
            body={"removeLabelIds": [label_id]},
        ).execute()
        return True
    except Exception as e:
        # Non-fatal -- we still proceed with Dropbox + table deletion.
        print(f"  [warn] label remove failed for thread {thread_id}: {e}")
        return False


# --- Main ------------------------------------------------------------------

def main():
    print(f"=== reverse-auto-filed-quotes starting (DRY_RUN={DRY_RUN}) ===")
    # 1. Pull every row in quote_files_cloud
    st, body = _sb("GET", f"{TABLE}?select=*&order=filed_at.asc&limit=10000")
    if st != 200:
        raise SystemExit(f"quote_files_cloud GET failed: HTTP {st}: {body[:200]}")
    rows = json.loads(body)
    print(f"  {len(rows)} row(s) to reverse")
    if not rows:
        print("  nothing to do.")
        return

    dbx = dropbox_client()
    svc = gmail_service()
    auto_label_id = find_label_id(svc, "AUTO-FILED")
    if auto_label_id:
        print(f"  AUTO-FILED label id: {auto_label_id}")
    else:
        print("  AUTO-FILED label not found -- skipping label removal step")

    seen_threads: set[str] = set()
    deleted_files = 0
    skipped_files = 0
    removed_rows = 0

    for r in rows:
        rid       = r.get("id")
        dbx_path  = r.get("dropbox_path") or ""
        msg_id    = r.get("message_id")    or ""
        thread_id = r.get("thread_id")     or ""
        fname     = r.get("filename")      or dbx_path.rsplit("/", 1)[-1]

        # Drop file in Dropbox
        if dbx_path:
            ok, detail = dropbox_delete(dbx, dbx_path)
            if ok:
                deleted_files += 1
                print(f"  [del] {dbx_path}  ({detail})")
            else:
                skipped_files += 1
                print(f"  [skip] {dbx_path}  ({detail})")
        else:
            skipped_files += 1
            print(f"  [skip] no dropbox_path on row id={rid}")

        # Remove AUTO-FILED label from the source thread (once per thread).
        if auto_label_id and thread_id and thread_id not in seen_threads:
            seen_threads.add(thread_id)
            remove_label_from_thread(svc, thread_id, auto_label_id)

        # Delete the audit row
        if not DRY_RUN and rid is not None:
            try:
                _sb("DELETE", f"{TABLE}?id=eq.{urllib.parse.quote(str(rid), safe='')}")
                removed_rows += 1
            except Exception as e:
                print(f"  [warn] DELETE row id={rid} failed: {e}")

    print(f"\n=== done ===")
    print(f"  deleted from Dropbox: {deleted_files}")
    print(f"  skipped:              {skipped_files}")
    print(f"  rows removed:         {removed_rows}")
    print(f"  threads un-labeled:   {len(seen_threads)}")
    if DRY_RUN:
        print("\n  DRY_RUN was set -- nothing was actually changed.")


if __name__ == "__main__":
    main()
