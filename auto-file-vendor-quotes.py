"""auto-file-vendor-quotes.py -- Cron that pulls vendor-quote attachments
from each active bid's Gmail label into the bid's Dropbox QUOTES folder.

Per memory note `project_auto_quote_filer.md`:
> future cron: pull attachments from each bid's Gmail label, rename per
> `<Vendor> - <Scope> - <date>` convention, drop into Dropbox <bid>/QUOTES/.

V1 (this script):
- Vendor inferred from sender display name / email domain
- No AI scope detection -- keep original filename, prefix with "<Vendor> - <YYYY-MM-DD> - "
- Idempotent via quote_files_cloud (keyed on message_id + attachment_id)
- Skips emails from Fusion (no point filing our own quotes)
- Skips obvious non-quote attachments (.ics, signature images < 50KB)

Required env: SUPABASE_SERVICE_KEY, GMAIL_TOKEN_JSON,
              DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

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
QUOTE_FILES_TABLE = "quote_files_cloud"
ACTIVE_BIDS_TABLES = ("prebid_bids_cloud", "bid_setup_completions_cloud")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Domains we consider Fusion's own (skip filing our own attachments).
FUSION_DOMAINS = {
    "fusionelectric-inc.com",
    "fusionelectricinc.com",
    "fusionelectric.com",
}
# Mime types we skip (calendar invites, tiny inline images, signature blocks)
SKIP_MIMES = {
    "text/calendar", "application/ics", "text/x-vcalendar",
}
# Minimum size to consider a real quote attachment (bytes). Below this is
# usually a signature image or icon.
MIN_ATTACHMENT_BYTES = 5 * 1024


# --- Supabase --------------------------------------------------------------

def _service_key():
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb(method, path, body=None, extra=None, timeout=30):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": _service_key(),
        "Authorization": f"Bearer {_service_key()}",
        "content-type": "application/json",
    }
    if extra:
        headers.update(extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def fetch_active_bids():
    """Get bids that are still in-flight: prebid_bids_cloud rows whose
    notes don't contain local_helper:complete (still being set up) UNION
    bid_setup_completions_cloud rows from the last 90 days. We label the
    quotes by EST# + label from prebid (which has the gmail_label we need)."""
    qs = ("select=id,est_number,project_name,gmail_label,dropbox_folder,notes,"
          "created_at,updated_at&order=updated_at.desc&limit=200")
    st, body = _sb("GET", f"prebid_bids_cloud?{qs}")
    if st != 200:
        raise SystemExit(f"prebid_bids_cloud GET failed: HTTP {st} {body[:200]!r}")
    rows = json.loads(body)
    # Skip rows explicitly marked complete (we still want to file quotes
    # for those? Yes -- vendors keep emailing after setup. Filter only
    # 'skip' so we don't fill folders for cancelled bids).
    out = []
    for r in rows:
        notes = (r.get("notes") or "").lower()
        if "local_helper:skip" in notes:
            continue
        if not r.get("gmail_label"):
            continue
        if not r.get("dropbox_folder"):
            continue
        out.append(r)
    return out


def already_filed_keys(bid_id: str) -> set[str]:
    """Return set of "{message_id}::{attachment_id}" already filed for this bid."""
    qs = f"select=id&bid_id=eq.{urllib.parse.quote(bid_id, safe='')}&limit=10000"
    st, body = _sb("GET", f"{QUOTE_FILES_TABLE}?{qs}")
    if st != 200:
        # Table may not exist -- treat as empty
        return set()
    return {r["id"] for r in json.loads(body)}


def upsert_quote_file(row: dict) -> None:
    st, resp = _sb("POST", QUOTE_FILES_TABLE, body=[row],
                   extra={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if st not in (200, 201, 204):
        print(f"  [warn] quote_files_cloud upsert HTTP {st}: {resp[:200]!r}",
              file=sys.stderr)


# --- Gmail ------------------------------------------------------------------

def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def find_or_create_label(svc, name: str) -> str | None:
    """Return label ID for a label name, creating it if missing."""
    try:
        labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    except Exception as e:
        print(f"  [warn] labels list failed: {e}")
        return None
    for L in labels:
        if L.get("name") == name:
            return L.get("id")
    try:
        created = svc.users().labels().create(
            userId="me",
            body={"name": name,
                  "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        return created.get("id")
    except Exception as e:
        print(f"  [warn] label create failed: {e}")
        return None


def messages_for_label(svc, label_name: str) -> list[dict]:
    """Return message stubs (id+threadId) for all messages with the label."""
    out = []
    page_token = None
    while True:
        try:
            resp = svc.users().messages().list(
                userId="me",
                q=f'label:"{label_name}"',
                maxResults=200,
                pageToken=page_token,
            ).execute()
        except Exception as e:
            print(f"  [warn] message list failed for label {label_name!r}: {e}")
            return out
        out.extend(resp.get("messages") or [])
        page_token = resp.get("nextPageToken")
        if not page_token: break
    return out


def get_message_full(svc, msg_id: str) -> dict | None:
    try:
        return svc.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except Exception as e:
        print(f"  [warn] get message {msg_id} failed: {e}")
        return None


def header(msg: dict, name: str) -> str:
    for h in (msg.get("payload", {}).get("headers") or []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def parse_sender(from_hdr: str) -> tuple[str, str]:
    """Return (display_name, email)."""
    name, email = parseaddr(from_hdr)
    return (name or "", email or "")


def vendor_from_sender(display: str, email: str) -> str:
    """Best-effort: pull a clean vendor name from the From line."""
    # Prefer the display name's company-ish portion
    if display:
        # Common pattern: "John Smith - Graybar" or "Graybar | Sacramento"
        for sep in (" - ", " | ", ", "):
            if sep in display:
                # Take the LONGER side (tends to be company)
                parts = [p.strip() for p in display.split(sep) if p.strip()]
                parts.sort(key=len, reverse=True)
                if parts: return parts[0]
        return display.strip()
    # Fallback: domain
    if "@" in email:
        domain = email.split("@", 1)[1].split(".")[0]
        return domain.replace("-", " ").title()
    return "Unknown"


def is_fusion_email(email: str) -> bool:
    return any(email.lower().endswith("@" + d) for d in FUSION_DOMAINS)


def extract_attachments(svc, msg: dict) -> list[dict]:
    """Walk the MIME tree; return list of {attachment_id, filename,
    mime_type, size_bytes, body_data?}."""
    out = []
    msg_id = msg.get("id")

    def walk(part):
        if part.get("parts"):
            for p in part["parts"]:
                walk(p)
            return
        body = part.get("body") or {}
        if not body.get("attachmentId"):
            return  # inline / no attachment
        filename = part.get("filename") or ""
        if not filename:
            return  # no filename = inline
        mime = part.get("mimeType") or ""
        size = body.get("size") or 0
        out.append({
            "attachment_id": body["attachmentId"],
            "filename": filename,
            "mime_type": mime,
            "size_bytes": size,
            "msg_id": msg_id,
        })

    walk(msg.get("payload") or {})
    return out


def fetch_attachment_bytes(svc, msg_id: str, att_id: str) -> bytes | None:
    try:
        resp = svc.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=att_id,
        ).execute()
    except Exception as e:
        print(f"  [warn] fetch attachment {att_id} failed: {e}")
        return None
    data = resp.get("data") or ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def add_label(svc, msg_id: str, label_id: str) -> None:
    try:
        svc.users().messages().modify(
            userId="me", id=msg_id,
            body={"addLabelIds": [label_id]},
        ).execute()
    except Exception as e:
        print(f"  [warn] addLabel failed for {msg_id}: {e}")


# --- Dropbox ---------------------------------------------------------------

def dropbox_client():
    refresh = (os.environ.get("DROPBOX_REFRESH_TOKEN") or "").strip()
    app_key = (os.environ.get("DROPBOX_APP_KEY") or "").strip()
    app_secret = (os.environ.get("DROPBOX_APP_SECRET") or "").strip()
    if not (refresh and app_key and app_secret):
        raise SystemExit("DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET all required.")
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=refresh,
        app_key=app_key, app_secret=app_secret, timeout=60,
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
    return dbx


def ensure_quotes_folder(dbx, bid_folder: str) -> str:
    """Ensure <bid_folder>/QUOTES/ exists; return its path."""
    target = bid_folder.rstrip("/") + "/QUOTES"
    try:
        dbx.files_get_metadata(target)
        return target
    except Exception:
        pass
    try:
        dbx.files_create_folder_v2(target)
    except Exception as e:
        msg = str(e).lower()
        if "conflict" not in msg:  # already exists is fine
            print(f"  [warn] create QUOTES failed: {e}")
            raise
    return target


def upload_to_dropbox(dbx, dest_path: str, data: bytes) -> None:
    from dropbox.files import WriteMode
    dbx.files_upload(data, dest_path, mode=WriteMode("overwrite"), mute=True)


# --- Naming ----------------------------------------------------------------

SAFE_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_for_filename(s: str, max_len: int = 60) -> str:
    s = SAFE_RE.sub(" ", s).strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > max_len:
        s = s[: max_len].strip()
    return s


def filed_filename(vendor: str, msg_date: datetime, original: str) -> str:
    """Construct '<Vendor> - YYYY-MM-DD - <original>' with safety."""
    date_part = msg_date.strftime("%Y-%m-%d") if msg_date else "0000-00-00"
    vendor_clean = sanitize_for_filename(vendor or "Unknown Vendor", 40)
    name, ext = os.path.splitext(original or "attachment")
    name_clean = sanitize_for_filename(name, 70)
    if not ext: ext = ".bin"
    return f"{vendor_clean} - {date_part} - {name_clean}{ext}"


# --- Main ------------------------------------------------------------------

def process_bid(svc, dbx, filed_label_id: str, bid: dict) -> int:
    """Process one bid; return number of attachments newly filed."""
    bid_id = bid.get("id") or "?"
    est_number = bid.get("est_number") or "?"
    label = bid.get("gmail_label") or ""
    dbx_folder = bid.get("dropbox_folder") or ""

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] bid {bid_id} (EST# {est_number}) "
          f"label={label!r}")

    msgs = messages_for_label(svc, label)
    if not msgs:
        print(f"  no messages with label")
        return 0
    print(f"  {len(msgs)} message(s) on label")

    already = already_filed_keys(bid_id)
    quotes_path = None  # lazy-create the QUOTES folder only if we have files
    filed = 0

    for stub in msgs:
        msg_id = stub.get("id")
        if not msg_id: continue
        full = get_message_full(svc, msg_id)
        if not full: continue
        attachments = extract_attachments(svc, full)
        if not attachments: continue

        # Skip if this message is from Fusion themselves
        from_hdr = header(full, "From")
        display, email = parse_sender(from_hdr)
        if is_fusion_email(email):
            continue
        vendor = vendor_from_sender(display, email)

        # Message date
        date_hdr = header(full, "Date")
        try:
            msg_date = parsedate_to_datetime(date_hdr) if date_hdr else None
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
        except Exception:
            msg_date = None

        for att in attachments:
            key = f"{msg_id}::{att['attachment_id']}"
            if key in already:
                continue  # already filed

            mime = (att.get("mime_type") or "").lower()
            if any(mime.startswith(s) for s in SKIP_MIMES):
                continue
            if (att.get("size_bytes") or 0) < MIN_ATTACHMENT_BYTES:
                continue

            # Lazily create the QUOTES folder
            if quotes_path is None:
                try:
                    quotes_path = ensure_quotes_folder(dbx, dbx_folder)
                except Exception as e:
                    print(f"  [warn] cannot create QUOTES folder: {e} -- aborting bid")
                    return filed

            data = fetch_attachment_bytes(svc, msg_id, att["attachment_id"])
            if not data:
                continue
            outname = filed_filename(vendor, msg_date or datetime.now(timezone.utc),
                                     att["filename"])
            dest_path = quotes_path + "/" + outname
            try:
                upload_to_dropbox(dbx, dest_path, data)
            except Exception as e:
                print(f"  [warn] upload {outname} failed: {e}")
                continue

            row = {
                "id": key,
                "bid_id": bid_id,
                "est_number": est_number,
                "gmail_label": label,
                "gmail_message_id": msg_id,
                "gmail_thread_id": full.get("threadId"),
                "attachment_id": att["attachment_id"],
                "vendor": vendor,
                "vendor_email": email,
                "filename": att["filename"],
                "filed_filename": outname,
                "dropbox_path": dest_path,
                "size_bytes": att["size_bytes"],
                "message_date": (msg_date.isoformat() if msg_date else None),
                "filed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            upsert_quote_file(row)
            filed += 1
            print(f"  + {outname}  ({att['size_bytes']} bytes)")

        # Tag the thread as filed (so the user sees it in Gmail)
        if filed_label_id and any(
            f"{msg_id}::{a['attachment_id']}" not in already
            for a in attachments
        ):
            add_label(svc, msg_id, filed_label_id)

    if filed:
        print(f"  filed {filed} new attachment(s)")
    else:
        print(f"  no new attachments")
    return filed


def main():
    print(f"=== auto-file-vendor-quotes started at {datetime.now().isoformat()} ===")
    svc = gmail_service()
    dbx = dropbox_client()

    # Find/create the "FILED-AUTO" Gmail label so we can tag processed threads
    filed_label_id = find_or_create_label(svc, "AUTO-FILED")

    bids = fetch_active_bids()
    print(f"{len(bids)} active bid(s) with gmail_label + dropbox_folder")

    total = 0
    for b in bids:
        try:
            total += process_bid(svc, dbx, filed_label_id, b)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  [err] bid {b.get('id')!r}: {e}")
    print(f"\n=== done; {total} new file(s) across {len(bids)} bid(s) ===")


if __name__ == "__main__":
    main()
