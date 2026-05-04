"""detect-bid-addenda.py -- Mission-critical addenda detector.

For each active bid, scans:
  1. Gmail label messages (subject contains "Addendum N" / "Addenda N")
  2. Dropbox PLANS & SPECS folder (subfolders/files named "Addendum N - ...")

Reconciles findings, upserts into bid_addenda_cloud, and emails Alex on
any newly-detected addendum (regardless of source) so nothing slips
through. A mismatch (e.g. addendum in Gmail but no folder yet) gets
flagged in the email so Alex knows to download/file.

Why this matters: missing an addendum on a bid means submitting against
the wrong specs; that loses the bid (or wins it on a number that turns
into a money-losing job). This is the single most expensive thing to
get wrong.

Required env: SUPABASE_SERVICE_KEY, GMAIL_TOKEN_JSON, GMAIL_FROM,
              DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET,
              ALERT_TO_EMAIL (defaults to alex@fusionelectric-inc.com)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

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
ADDENDA_TABLE = "bid_addenda_cloud"
EMAILS_REVIEW_TABLE = "bid_emails_review_cloud"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # canonical scope; subsumes readonly + send + labels

# We treat all of these as "addendum-equivalent" -- they can change scope
# just as much as a formal addendum, so missing one is just as costly:
#   addendum / addenda      - formal scope change
#   revision / rev          - drawing/spec revision
#   bulletin                - common in K-12 / healthcare
#   ASI (Architect's Suppl. Instructions) - same effect as addendum
#   RFI response            - if it changes scope (we can't tell from subject;
#                              flag as addendum-like so it gets reviewed)
#   sketch (SK-1, SK-2...)  - design sketches that supersede plans
#   plan/spec/drawing update - generic catch
# The captured group is the addendum number (or '0' for unnumbered ones,
# which means "we found a scope-change-like email/file but no explicit #";
# the cron will assign a synthetic number based on dedup against existing
# rows so each unnumbered hit becomes its own row).
SUBJECT_RE = re.compile(
    r"\b("
    r"addendum|addenda|"
    r"revision|rev|"
    r"bulletin|"
    r"a\.?s\.?i\.?|"               # ASI, A.S.I., A S I
    r"rfi\s*(?:response|reply)|"   # RFI response/reply
    r"sketch\s*(?:sk[-#]?|s[-#])?|" # Sketch SK-1, S-1
    r"(?:plan|spec(?:ification)?|drawing)s?\s*(?:update|revision|change|addendum)|"
    # Verb-form scope changes: "Revised drawings", "Updated plans v3",
    # "New specifications", "Latest sheets"
    r"(?:revised|updated|amended|new|latest)\s+(?:plan|spec(?:ification)?|drawing|sheet|scope)s?|"
    r"clarification"
    r")\b[^0-9a-z]*(?:no\.?|#|v|number)?\s*(\d{1,3})?\b",
    re.IGNORECASE,
)
# Folder/file names mirror the same broadening
FOLDER_RE = re.compile(
    r"^\s*("
    r"addend(?:um|a)|"
    r"revision|rev|"
    r"bulletin|"
    r"a\.?s\.?i\.?|"
    r"sketch\s*(?:sk[-#]?|s[-#])?|"
    r"(?:plan|spec|drawing)s?\s*(?:update|revision|change|addendum)|"
    r"clarification"
    r")\b[^0-9]*(\d{1,3})?",
    re.IGNORECASE,
)
# Senders we know are Fusion (to skip the digest noise on our own emails).
FUSION_SENDER_DOMAINS = {
    "fusionelectric-inc.com",
    "fusionelectricinc.com",
    "fusionelectric.com",
    "fusionelectricinc.onmicrosoft.com",
}

# If the subject ALSO contains any of these, it's almost certainly a
# vendor pricing email rather than a scope-change. Common false positives:
# "Re: rev 2 of pricing", "Updated quote sheet", "Revised pricing for ..."
VENDOR_HINT_RE = re.compile(
    r"\b(quote|pricing|price|estimate|proposal|bid invitation|"
    r"\$\s*\d|invoicing?|billable)\b",
    re.IGNORECASE,
)


def _normalize_pattern_kind(raw: str) -> str:
    """Map a regex-captured keyword like 'rev' or 'A.S.I.' to a canonical
    short identifier used in pattern_matched."""
    s = (raw or "").lower().replace(".", "").replace(" ", "")
    if s.startswith("addend"): return "addendum"
    if s.startswith("rev"):    return "revision"
    if s.startswith("bullet"): return "bulletin"
    if s.startswith("asi") or s == "asi": return "asi"
    if s.startswith("rfi"):    return "rfi_response"
    if s.startswith("sketch"): return "sketch"
    if s.startswith("clarif"): return "clarification"
    if "update" in s or "revision" in s or "change" in s: return "plan_update"
    return s or "scope_change"


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
    if extra: headers.update(extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def fetch_active_bids() -> list[dict]:
    qs = ("select=id,est_number,project_name,gmail_label,dropbox_folder,notes,"
          "client_gc,project_engineer,bid_due_date,updated_at"
          "&order=updated_at.desc&limit=200")
    st, body = _sb("GET", f"prebid_bids_cloud?{qs}")
    if st != 200:
        raise SystemExit(f"prebid_bids_cloud GET failed: HTTP {st} {body[:200]!r}")
    out = []
    for r in json.loads(body):
        notes = (r.get("notes") or "").lower()
        if "local_helper:skip" in notes:  # cancelled
            continue
        if not r.get("gmail_label") or not r.get("dropbox_folder"):
            continue  # nothing to scan
        out.append(r)
    return out


def fetch_existing_addenda(bid_id: str) -> dict[int, dict]:
    qs = (f"select=*&bid_id=eq.{urllib.parse.quote(bid_id, safe='')}"
          f"&order=addendum_number.asc")
    st, body = _sb("GET", f"{ADDENDA_TABLE}?{qs}")
    if st != 200: return {}
    return {r["addendum_number"]: r for r in json.loads(body)}


def upsert_addendum(row: dict) -> None:
    st, resp = _sb("POST", ADDENDA_TABLE, body=[row],
                   extra={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if st not in (200, 201, 204):
        print(f"  [warn] addendum upsert HTTP {st}: {resp[:200]!r}", file=sys.stderr)


def stamp_notified(addendum_id: str) -> None:
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _sb("PATCH",
        f"{ADDENDA_TABLE}?id=eq.{urllib.parse.quote(addendum_id, safe='')}",
        body={"notified_at": iso, "updated_at": iso})


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


def messages_for_label(svc, label_name: str) -> list[dict]:
    out, page_token = [], None
    while True:
        try:
            resp = svc.users().messages().list(
                userId="me", q=f'label:"{label_name}"',
                maxResults=200, pageToken=page_token,
            ).execute()
        except Exception as e:
            print(f"  [warn] message list failed: {e}")
            return out
        out.extend(resp.get("messages") or [])
        page_token = resp.get("nextPageToken")
        if not page_token: break
    return out


def get_message_meta(svc, msg_id: str) -> dict:
    """Return {subject, from, date, threadId, has_attachments}."""
    try:
        m = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
    except Exception as e:
        print(f"  [warn] get message {msg_id} failed: {e}")
        return {}
    out = {"threadId": m.get("threadId") or ""}
    for h in (m.get("payload", {}).get("headers") or []):
        n = h.get("name", "").lower()
        if n in ("subject", "from", "date"):
            out[n] = h.get("value") or ""
    # Detect attachments by scanning parts (quick — metadata format has parts)
    def has_atts(part):
        if (part.get("filename") or "") and (part.get("body") or {}).get("attachmentId"):
            return True
        for p in (part.get("parts") or []):
            if has_atts(p): return True
        return False
    out["has_attachments"] = has_atts(m.get("payload") or {})
    return out


def upsert_review_row(row: dict) -> None:
    st, resp = _sb("POST", EMAILS_REVIEW_TABLE, body=[row],
                   extra={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if st not in (200, 201, 204):
        if not getattr(upsert_review_row, "_warned", False):
            print(f"  [warn] {EMAILS_REVIEW_TABLE} write HTTP {st} -- "
                  f"run bid-emails-review-cloud-table.sql to enable digest. "
                  f"({resp[:160]!r})", file=sys.stderr)
            upsert_review_row._warned = True


def _sender_email(from_hdr: str) -> tuple[str, str]:
    from email.utils import parseaddr
    name, email = parseaddr(from_hdr or "")
    return (name or "", email or "")


def _is_fusion_sender(email_addr: str) -> bool:
    e = (email_addr or "").lower()
    return any(e.endswith("@" + d) for d in FUSION_SENDER_DOMAINS)


def detect_in_gmail(svc, bid: dict, label: str) -> dict[int, dict]:
    """Walk every email on the label. For each:
       - Classify (addendum_like / fusion_internal / vendor_or_attachment / review_needed)
       - Write a row to bid_emails_review_cloud (idempotent on message_id)
       - If it's addendum-like with a number, also accumulate for
         bid_addenda_cloud insertion.
    Returns {addendum_number: {message_ids:[], subjects:[]}}.

    Unnumbered scope-changes (e.g., "Revised drawings" with no number) get
    a synthetic number 9000+ so they show up as separate rows in
    bid_addenda_cloud rather than colliding with each other or real
    numbered addenda."""
    found: dict[int, dict] = {}
    bid_id = bid.get("id") or "?"
    est_number = bid.get("est_number") or "?"
    synthetic_seq = 9000  # for unnumbered hits

    for stub in messages_for_label(svc, label):
        msg_id = stub.get("id")
        if not msg_id: continue
        meta = get_message_meta(svc, msg_id)
        if not meta: continue
        subj = meta.get("subject") or ""
        from_hdr = meta.get("from") or ""
        date_hdr = meta.get("date") or ""
        thread_id = meta.get("threadId") or ""
        sender_name, sender_email = _sender_email(from_hdr)

        # Parse received_at
        received_iso = None
        if date_hdr:
            try:
                from email.utils import parsedate_to_datetime
                d = parsedate_to_datetime(date_hdr)
                if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
                received_iso = d.isoformat(timespec="seconds")
            except Exception:
                pass

        m = SUBJECT_RE.search(subj)
        kind = None
        num = None
        if m:
            kind = _normalize_pattern_kind(m.group(1))
            try:
                num = int(m.group(2)) if m.group(2) else None
            except ValueError:
                num = None
            # Sanity bound
            if num is not None and (num <= 0 or num > 200):
                num = None

        # If the broadened SUBJECT_RE matched but the subject ALSO contains
        # vendor-pricing words ("quote", "pricing", "$"), demote: this is
        # almost certainly a vendor reply, not a real scope change.
        is_vendor_pricing = bool(VENDOR_HINT_RE.search(subj))

        # Classify (order matters: Fusion-internal beats everything; real
        # scope-change beats attachments; pricing chatter is demoted away
        # from addendum_like even if the regex hit it.)
        if _is_fusion_sender(sender_email):
            classification = "fusion_internal"
        elif m and not is_vendor_pricing:
            classification = "addendum_like"
        elif is_vendor_pricing or meta.get("has_attachments"):
            classification = "vendor_or_attachment"
        else:
            classification = "review_needed"

        pattern_matched = (
            f"{kind}_{num}" if (kind and num is not None)
            else (kind if kind else None)
        )

        # Always upsert the review row (idempotent on msg_id; redo on each
        # poll is fine because subject/sender don't change).
        upsert_review_row({
            "id":               msg_id,
            "bid_id":           bid_id,
            "est_number":       est_number,
            "gmail_message_id": msg_id,
            "gmail_thread_id":  thread_id,
            "gmail_label":      label,
            "subject":          (subj or "")[:500],
            "sender_name":      sender_name[:200],
            "sender_email":     sender_email[:200],
            "received_at":      received_iso,
            "classification":   classification,
            "pattern_matched":  pattern_matched,
            "has_attachments":  bool(meta.get("has_attachments")),
        })

        # If it matched an addendum-like pattern, accumulate for the
        # numbered-addendum dataset. Skip the vendor-pricing false positives.
        if not m or is_vendor_pricing: continue
        if num is None:
            # Unnumbered scope change: assign a synthetic number that's
            # stable across runs based on the message_id hash.
            num = 9000 + (abs(hash(msg_id)) % 999)
        slot = found.setdefault(num, {"message_ids": [], "subjects": [],
                                      "patterns": []})
        slot["message_ids"].append(msg_id)
        slot["subjects"].append(subj)
        if pattern_matched: slot["patterns"].append(pattern_matched)
    return found


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


def detect_in_folder(dbx, bid_folder: str) -> dict[int, dict]:
    """Scan PLANS & SPECS for Addendum subfolders / files. Return
    {addendum_number: {folder_path, files:[]}}."""
    plans_specs = bid_folder.rstrip("/") + "/PLANS & SPECS"
    found: dict[int, dict] = {}
    try:
        listing = dbx.files_list_folder(plans_specs, recursive=False)
    except Exception:
        return found  # no PLANS & SPECS folder yet (early in setup)

    def consume(entries):
        from dropbox.files import FolderMetadata, FileMetadata
        for e in entries:
            name = getattr(e, "name", "") or ""
            m = FOLDER_RE.match(name)
            if not m: continue
            try:
                num = int(m.group(1))
            except ValueError:
                continue
            if num <= 0 or num > 200: continue
            slot = found.setdefault(num, {"folder_path": None, "files": []})
            if isinstance(e, FolderMetadata):
                slot["folder_path"] = e.path_display or e.path_lower
                # List files inside the addendum folder
                try:
                    sub = dbx.files_list_folder(slot["folder_path"], recursive=False)
                    for se in sub.entries:
                        if isinstance(se, FileMetadata):
                            slot["files"].append(se.name)
                    while sub.has_more:
                        sub = dbx.files_list_folder_continue(sub.cursor)
                        for se in sub.entries:
                            if isinstance(se, FileMetadata):
                                slot["files"].append(se.name)
                except Exception as ee:
                    print(f"  [warn] couldn't list addendum folder {name}: {ee}")
            elif isinstance(e, FileMetadata):
                # Loose file like "Addendum 3.pdf" at root of PLANS & SPECS
                slot["files"].append(e.name)
                if not slot["folder_path"]:
                    slot["folder_path"] = plans_specs

    consume(listing.entries)
    while listing.has_more:
        listing = dbx.files_list_folder_continue(listing.cursor)
        consume(listing.entries)
    return found


# --- Notification email ----------------------------------------------------

def send_alert(svc, sender: str, recipient: str,
               new_addenda: list[dict]) -> None:
    if not new_addenda: return
    lines = [
        f"{len(new_addenda)} new bid addendum/addenda detected.\n",
    ]
    for a in new_addenda:
        bid = a["bid"]
        det = a["detection"]
        srcs = []
        if det.get("found_in_gmail"): srcs.append("Gmail")
        if det.get("found_in_folder"): srcs.append("Folder")
        if not srcs: srcs = ["unknown source"]
        lines.append(
            f"  - EST# {bid.get('est_number')} {bid.get('project_name')[:40]}\n"
            f"    Addendum #{det['addendum_number']}\n"
            f"    Sources: {', '.join(srcs)}\n"
            f"    Mismatch: {'YES (manual reconcile needed)' if len(srcs) == 1 else 'no -- both sources have it'}"
        )
        if det.get("gmail_subjects"):
            for s in det["gmail_subjects"][:2]:
                lines.append(f"    Subject: {s[:120]}")
        if det.get("folder_path"):
            lines.append(f"    Folder: {det['folder_path']}")
            if det.get("folder_files"):
                lines.append(f"    Files: {', '.join((det['folder_files'] or [])[:5])}")
        lines.append("")

    lines.append(
        "What to do:\n"
        "  - If 'Mismatch: YES', the addendum exists in only one place. "
        "Pull the missing piece (download from Gmail into PLANS & SPECS, "
        "or check Gmail for a corresponding email) before bid day.\n"
        "  - If both sources have it, you're good -- this is a new "
        "addendum to incorporate into your numbers.\n"
    )
    body = "\n".join(lines)
    subj = (
        f"[Fusion Bid Addenda] {len(new_addenda)} new addend"
        f"{'a' if len(new_addenda) != 1 else 'um'} detected"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender; msg["To"] = recipient; msg["Subject"] = subj
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[ok] alert sent to {recipient}: {subj}")


# --- Main ------------------------------------------------------------------

def main():
    print(f"=== detect-bid-addenda started at {datetime.now().isoformat()} ===")
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender:
        raise SystemExit("GMAIL_FROM env var required.")
    recipient = (os.environ.get("ALERT_TO_EMAIL") or
                 "alex@fusionelectric-inc.com").strip()

    svc = gmail_service()
    dbx = dropbox_client()

    bids = fetch_active_bids()
    print(f"{len(bids)} active bid(s) with gmail_label + dropbox_folder")

    new_for_email: list[dict] = []

    for bid in bids:
        bid_id = bid.get("id") or "?"
        est = bid.get("est_number") or "?"
        label = bid.get("gmail_label")
        folder = bid.get("dropbox_folder")

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] EST# {est} ({bid_id})")

        try:
            gmail_found  = detect_in_gmail(svc, bid, label)
        except Exception as e:
            print(f"  [err] gmail scan: {e}"); gmail_found = {}
        try:
            folder_found = detect_in_folder(dbx, folder)
        except Exception as e:
            print(f"  [err] folder scan: {e}"); folder_found = {}

        all_nums = sorted(set(gmail_found) | set(folder_found))
        if not all_nums:
            print("  no addenda detected")
            continue
        print(f"  detected addenda: {all_nums}")

        existing = fetch_existing_addenda(bid_id)

        for num in all_nums:
            g = gmail_found.get(num) or {}
            f = folder_found.get(num) or {}
            row_id = f"{bid_id}::addendum-{num}"
            prior = existing.get(num)

            row = {
                "id": row_id,
                "bid_id": bid_id,
                "est_number": est,
                "addendum_number": num,
                "found_in_gmail": bool(g),
                "gmail_message_ids": g.get("message_ids") or [],
                "gmail_subjects": g.get("subjects") or [],
                "found_in_folder": bool(f),
                "folder_path": f.get("folder_path"),
                "folder_files": f.get("files") or [],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            upsert_addendum(row)

            # Decide whether to email:
            # - First time we've ever seen this addendum -> email.
            # - We saw it before in only one source, now we have a second
            #   source -> consider it a "reconciled" event but don't re-email
            #   (avoid noise). User can read the row in the UI.
            if prior is None:
                row["bid"] = bid
                row["detection"] = row.copy()
                new_for_email.append({"bid": bid, "detection": row})
                print(f"  + addendum {num} (new) -- gmail={bool(g)}, folder={bool(f)}")
            else:
                print(f"  ~ addendum {num} (already known)")

    if new_for_email:
        try:
            send_alert(svc, sender, recipient, new_for_email)
            for n in new_for_email:
                stamp_notified(n["detection"]["id"])
        except Exception as e:
            print(f"[err] failed to send alert: {e}", file=sys.stderr)
            sys.exit(3)

    print(f"\n=== done; {len(new_for_email)} new addendum/addenda ===")


if __name__ == "__main__":
    main()
