"""Poll Supabase for unread suggestion-box submissions and email them to
the host using the existing Gmail OAuth token (no SMTP / app password
required).

Flow on each run:
  1. SELECT * FROM suggestions WHERE notified_at IS NULL ORDER BY created_at
  2. For each row, build a host-readable email + Claude prompt body and
     send via the Gmail API.
  3. PATCH the row to set notified_at = now() so it isn't sent twice.

Wired into the 15-minute AutoUpdate-Task; latency = up to 15 min between
submission and email. Acceptable for an internal "tell-the-host"
suggestion box. To get instant email, set up a tighter cron or move this
into a Netlify Function with stored OAuth creds (more setup).

The Supabase anon key is safe to embed here — RLS already gates the
table. The token file (gmail-token.json) is local-only.
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# Reuse the parser's OAuth setup so the same token / credentials apply.
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Lazy import (only needed when actually sending).
try:
    from googleapiclient.discovery import build  # noqa: F401
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

CREDENTIALS_PATH = SCRIPTS_DIR / "gmail-credentials.json"
TOKEN_PATH = SCRIPTS_DIR / "gmail-token.json"
STATE_PATH = SCRIPTS_DIR / "suggestion-notify-state.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRsdHV2c2R3"
    "cnVqanNtaW90YXh5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzcwNDU4NDIsImV4cCI6MjA5MjYyMTg0Mn0."
    "_lMgcZgERcgVULQ87BQFrNpZBssJeNtqN5LhhGsqE8Y"
)

HOST_EMAIL = "alex@fusionelectric-inc.com"
SENDER_EMAIL = "alex@fusionelectric-inc.com"  # gmail-token is for this account


def get_gmail_service():
    """Load OAuth creds from env var (cloud / GitHub Actions) OR the
    local cached token file (PC). Env var wins when set so the same
    script runs unmodified in both environments.
    """
    creds = None
    token_json_env = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if token_json_env:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json_env), SCOPES)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] GMAIL_TOKEN_JSON env var malformed: {exc}", file=sys.stderr)
            return None
    elif TOKEN_PATH.is_file():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    else:
        print(f"[error] no Gmail token (env GMAIL_TOKEN_JSON unset, no file at {TOKEN_PATH})", file=sys.stderr)
        return None
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            print(f"[error] token refresh failed: {exc}", file=sys.stderr)
            return None
    if not creds.valid:
        print("[error] credentials invalid; cannot send", file=sys.stderr)
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def supabase_request(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1{path}"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def fetch_pending_suggestions():
    status, body = supabase_request("GET", "/suggestions?select=*&notified_at=is.null&order=created_at.asc")
    if status != 200:
        print(f"[error] supabase fetch failed: {status} {body}", file=sys.stderr)
        return []
    return body or []


def mark_notified(row_id):
    now = dt.datetime.utcnow().isoformat() + "Z"
    status, body = supabase_request(
        "PATCH",
        f"/suggestions?id=eq.{urllib.parse.quote(row_id)}",
        {"notified_at": now},
    )
    if status not in (200, 204):
        print(f"[warn] supabase patch failed for {row_id}: {status} {body}", file=sys.stderr)
        return False
    return True


def build_email_body(row):
    text = (row.get("text") or "").strip()
    author = (row.get("author") or "").strip() or "anonymous"
    page = (row.get("page") or "").strip() or "(unspecified)"
    created_at = row.get("created_at") or dt.datetime.utcnow().isoformat()
    try:
        dt_local = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        created_str = dt_local.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        created_str = created_at

    has_screenshot = bool(row.get("image_data"))
    subject = "[Fusion Portal Suggestion] " + text.replace("\n", " ").replace("\r", " ")[:60]
    body_lines = [
        "── SUGGESTION ──────────────────────────────",
        f"From: {author}",
        f"Page: {page}",
        f"Submitted: {created_str}",
    ]
    if has_screenshot:
        body_lines.append("Screenshot: attached below")
    body_lines += [
        "",
        text,
        "",
        "── PROMPT (paste into Claude Code in VS Code) ──",
        "",
        "You are working on the Fusion Portal codebase at C:\\Users\\AlexToler\\Documents\\Fusion Software. A user submitted the following suggestion via the in-app suggestion box and the host approved it:",
        "",
        f'"{text}"',
        "",
        "Please:",
        "1. Read the relevant page sources to understand the current behavior. The site has 5 pages:",
        "   • / — Main Panel (fusion-main-panel/src/index.html)",
        "   • /bid-panel/ — public Dave bid list (fusion-bid-list/site/bid-panel/index.html)",
        "   • /bay-bid-list/ — vendor quote tracker (fusion-bay-bid-list/src/index.html)",
        "   • /pm-panel/ — internal PM Panel (fusion-pm panel/src/index.html)",
        "   • /field-panel/ — field roster (fusion-field panel/src/index.html)",
        "2. Identify which page(s) the suggestion targets and the specific files/sections that need to change.",
        "3. Make the change in the source file (NOT in fusion-bid-list/site/* mirror copies — those are regenerated by Sync-PortalData.ps1).",
        "4. Verify the diff is minimal and targeted; preserve unrelated styles/logic.",
        "5. Bump the build label on the affected page (e.g. 2026.05.01-N).",
        "6. Deploy by running:",
        "   powershell -NoProfile -ExecutionPolicy Bypass -File \"C:\\Users\\AlexToler\\Documents\\Fusion Software\\fusion-pm panel\\Sync-PortalData.ps1\" -DeployOnChange",
        "7. Report back what changed and on which page(s). Site is live at https://fusion-main-panel.netlify.app.",
        "",
        "Confirm scope before doing anything destructive (URL renames, schema changes, deletes). For UI tweaks, just ship.",
    ]
    return subject, "\n".join(body_lines)


def _decode_data_url(data_url):
    """Parse a 'data:image/png;base64,XXXX' URL into (mime_subtype, bytes).
    Returns (None, None) if the input isn't a valid image data URL."""
    if not data_url or not isinstance(data_url, str):
        return None, None
    m = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", data_url, re.DOTALL)
    if not m:
        return None, None
    subtype = m.group(1).lower()
    try:
        return subtype, base64.b64decode(m.group(2), validate=False)
    except Exception:  # noqa: BLE001
        return None, None


def _build_message_id(row_id):
    """Synthetic Message-ID for the original email. We need a stable ID we
    can later put in In-Reply-To/References so Gmail threads the resolution
    reply with the original notification. The local-part is the row id;
    domain is a marker so it won't collide with real Google IDs."""
    return f"<sugg-{row_id}@fusion-portal.local>"


def send_email(
    service,
    subject,
    body_text,
    *,
    reply_to=None,
    image_data_url=None,
    message_id=None,
    in_reply_to=None,
    cc=None,
):
    """Send via Gmail. Optional knobs:
      - image_data_url: 'data:image/...;base64,...' string; attached inline.
      - message_id: explicit Message-ID header (lets us stamp a stable id we
        can reference later when sending the resolution reply).
      - in_reply_to: the prior email's Message-ID. When set, Gmail threads
        the new message under the original thanks to standard RFC headers.
      - cc: optional address (or comma-joined list) added as Cc — used so
        a non-host force-resolver (e.g. Gabriel) gets a copy of the
        threaded resolution reply for their own force edits.
    """
    subtype, img_bytes = _decode_data_url(image_data_url)
    if img_bytes:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        try:
            img = MIMEImage(img_bytes, _subtype=subtype)
            ext = subtype.split("+")[0] or "png"
            img.add_header("Content-Disposition", "inline", filename=f"suggestion-screenshot.{ext}")
            img.add_header("Content-ID", "<suggestion-screenshot>")
            msg.attach(img)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] failed to attach screenshot: {exc}", file=sys.stderr)
    else:
        msg = MIMEText(body_text, "plain", "utf-8")
    msg["From"] = f"Fusion Portal <{SENDER_EMAIL}>"
    msg["To"] = HOST_EMAIL
    if cc:
        msg["Cc"] = cc
    if reply_to and "@" in (reply_to or ""):
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # References should include the entire thread chain. For a
        # 1-deep reply, just the original Message-ID is fine.
        msg["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[error] gmail send failed: {exc}", file=sys.stderr)
        return False


def fetch_resolution_pending():
    """Rows that have been resolved but the host hasn't been emailed about
    the resolution yet. Threading happens via notification_message_id from
    the original email."""
    qs = "?select=*&resolved_at=not.is.null&resolution_notified_at=is.null&order=resolved_at.asc"
    status, body = supabase_request("GET", "/suggestions" + qs)
    if status != 200:
        print(f"[warn] supabase fetch (resolutions) failed: {status} {body}", file=sys.stderr)
        return []
    return body or []


def mark_resolution_notified(row_id):
    now = dt.datetime.utcnow().isoformat() + "Z"
    status, body = supabase_request(
        "PATCH",
        f"/suggestions?id=eq.{urllib.parse.quote(row_id)}",
        {"resolution_notified_at": now},
    )
    if status not in (200, 204):
        print(f"[warn] resolution patch failed for {row_id}: {status} {body}", file=sys.stderr)
        return False
    return True


def build_resolution_body(row):
    text = (row.get("text") or "").strip()
    note = (row.get("resolution_note") or "").strip() or "(no reply note)"
    by = (row.get("resolved_by") or "").strip() or "unknown"
    when = row.get("resolved_at") or ""
    try:
        d = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        when_str = d.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        when_str = when
    lines = [
        "── RESOLUTION ──────────────────────────────",
        f"Resolved by: {by}",
        f"Resolved at: {when_str}",
        "",
        f"Original suggestion:",
        f"  \"{text}\"",
        "",
        "Reply / what was done:",
        note,
    ]
    return "\n".join(lines)


def send_resolution_emails(service, rows):
    """Send a threaded reply to the host for each newly-resolved row.

    For force-resolved rows where the requester isn't the host (e.g.
    Gabriel triggered the auto-resolve), Cc the requester so they see what
    Claude actually did to their force edit. Manual resolutions still go
    only to the host.
    """
    if not rows:
        return 0
    sent = 0
    for row in rows:
        text = (row.get("text") or "").replace("\n", " ").replace("\r", " ")
        subject = "Re: [Fusion Portal Suggestion] " + text[:60]
        original_mid = row.get("notification_message_id") or _build_message_id(row.get("id"))
        body_text = build_resolution_body(row)

        # Cc the force-resolver if they aren't the host. force_resolve_by
        # is the canonical field; resolved_by gets stamped with a free-form
        # label like "auto-resolver (Claude Code)" by the agent and isn't
        # a real address, so we don't fall back to it for Cc.
        cc_addr = None
        forced_by = (row.get("force_resolve_by") or "").strip().lower()
        if forced_by and "@" in forced_by and forced_by != HOST_EMAIL.lower():
            cc_addr = forced_by

        ok = send_email(
            service,
            subject,
            body_text,
            reply_to=row.get("resolved_by") or row.get("author"),
            in_reply_to=original_mid,
            cc=cc_addr,
        )
        if ok and mark_resolution_notified(row.get("id")):
            sent += 1
            cc_note = f" (cc {cc_addr})" if cc_addr else ""
            try:
                print(f"  [ok] resolution email sent for {row.get('id')}{cc_note}")
            except UnicodeEncodeError:
                print(f"  [ok] resolution email sent for {row.get('id')}")
    return sent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Fetch pending but don't send or mark.")
    parser.add_argument("--limit", type=int, default=20, help="Max suggestions to send per run.")
    args = parser.parse_args()

    pending = fetch_pending_suggestions()
    pending = pending[: args.limit] if pending else []
    if pending:
        print(f"Found {len(pending)} pending suggestion(s).")
    else:
        print("No new suggestions to email.")

    if args.dry_run:
        for row in pending:
            print(f"  [dry-run] new suggestion {row.get('id')} from {row.get('author')!r}: {(row.get('text') or '')[:60]}")
        for row in fetch_resolution_pending():
            print(f"  [dry-run] resolution {row.get('id')}: {(row.get('resolution_note') or '')[:60]}")
        return 0

    service = get_gmail_service()
    if service is None:
        return 2

    sent = 0
    for row in pending:
        subject, body_text = build_email_body(row)
        image_data_url = row.get("image_data") or None
        message_id = _build_message_id(row.get("id"))
        ok = send_email(
            service,
            subject,
            body_text,
            reply_to=row.get("author"),
            image_data_url=image_data_url,
            message_id=message_id,
        )
        if ok and mark_notified(row.get("id")):
            # Stash the Message-ID so the eventual resolution reply can
            # be threaded under this email by Gmail.
            supabase_request(
                "PATCH",
                f"/suggestions?id=eq.{urllib.parse.quote(row.get('id'))}",
                {"notification_message_id": message_id},
            )
            sent += 1
            try:
                print(f"  [ok] emailed suggestion {row.get('id')} from {row.get('author')!r}")
            except UnicodeEncodeError:
                print(f"  [ok] emailed suggestion {row.get('id')}")
    print(f"\nSent {sent}/{len(pending)} suggestions.")

    # Resolution-reply pass: send a threaded follow-up to the host for any
    # row that was marked resolved since the last run.
    resolution_pending = fetch_resolution_pending()
    if resolution_pending:
        print(f"\nFound {len(resolution_pending)} resolution(s) to email.")
        r_sent = send_resolution_emails(service, resolution_pending)
        print(f"Sent {r_sent}/{len(resolution_pending)} resolution emails.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
