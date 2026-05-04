"""send-bid-emails-review-digest.py -- Daily digest of bid-label emails
that the addenda detector flagged as 'review_needed' (didn't match an
explicit scope-change pattern, didn't come from Fusion, didn't carry
attachments). These are the truly-ambiguous emails -- a sketch sent
without numbering, a "please use these revised drawings" note, etc.

We send ONE digest email per day grouping by bid, then stamp
digest_sent_at on each row so it doesn't repeat. Alex eyeballs the list
and decides what each one is.

Required env: SUPABASE_SERVICE_KEY, GMAIL_TOKEN_JSON, GMAIL_FROM,
              ALERT_TO_EMAIL (defaults to alex@fusionelectric-inc.com)
"""
from __future__ import annotations

import base64
import json
import os
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


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
TABLE = "bid_emails_review_cloud"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


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


def fetch_pending() -> list[dict]:
    qs = (
        "select=*&"
        "classification=eq.review_needed&"
        "digest_sent_at=is.null&"
        "order=est_number.asc,received_at.desc&"
        "limit=500"
    )
    st, body = _sb("GET", f"{TABLE}?{qs}")
    if st != 200:
        raise SystemExit(f"GET failed: HTTP {st} {body[:200]!r}")
    return json.loads(body)


def stamp_sent(ids: list[str]) -> None:
    if not ids: return
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # PATCH in chunks of 100 to keep URL length sane
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        in_clause = ",".join(urllib.parse.quote(x, safe='') for x in chunk)
        st, _ = _sb("PATCH", f"{TABLE}?id=in.({in_clause})",
                    body={"digest_sent_at": iso})
        if st not in (200, 204):
            print(f"[warn] stamp digest_sent_at chunk failed HTTP {st}",
                  file=sys.stderr)


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


def render_digest(rows: list[dict]) -> str:
    # Group by bid
    by_bid: dict[str, list[dict]] = {}
    for r in rows:
        key = f"{r.get('est_number') or '?'}::{r.get('bid_id') or '?'}"
        by_bid.setdefault(key, []).append(r)

    lines = [
        f"{len(rows)} email(s) on bid labels need a quick eyeball.",
        f"These didn't match any addendum/revision/bulletin/ASI pattern, "
        f"didn't come from Fusion, and don't carry attachments — but they "
        f"landed on a bid label, so something might be in them.",
        "",
        "Open each in Gmail and decide:",
        "  - Real scope change (sketch w/o number, 'use these revised plans', etc.)",
        "    -> mark as addendum manually & download attachments",
        "  - RFI question / clarification (no scope change yet)",
        "    -> respond / file under bid notes",
        "  - Vendor follow-up / pricing chatter -> ignore",
        "",
        "=" * 70,
    ]
    for key in sorted(by_bid.keys()):
        items = by_bid[key]
        est, _ = key.split("::", 1)
        lines.append(f"\nEST# {est}  ({len(items)} email{'s' if len(items)!=1 else ''})")
        for r in items:
            sender = r.get("sender_name") or r.get("sender_email") or "?"
            email = r.get("sender_email") or ""
            subj = (r.get("subject") or "(no subject)")[:120]
            recv = r.get("received_at") or ""
            lines.append(f"  - {sender} <{email}>")
            lines.append(f"    Subject: {subj}")
            if recv:
                lines.append(f"    Received: {recv}")
        lines.append("")

    lines.append("=" * 70)
    lines.append(
        "If any of these IS a scope change you missed, the addenda "
        "detector won't auto-promote -- you'll need to either rename "
        "the email subject to include 'Addendum N' (it'll catch on "
        "next run) or manually file the change."
    )
    lines.append("")
    lines.append("This digest fires once per day; rows are not re-sent.")
    return "\n".join(lines)


def send_digest(svc, sender: str, recipient: str, body: str, count: int) -> None:
    subj = (
        f"[Fusion Bid Review] {count} email"
        f"{'s' if count != 1 else ''} on bid labels need eyeballs"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender; msg["To"] = recipient; msg["Subject"] = subj
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[ok] digest sent: {subj}")


def main():
    print(f"=== send-bid-emails-review-digest started at {datetime.now().isoformat()} ===")
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender:
        raise SystemExit("GMAIL_FROM env var required.")
    recipient = (os.environ.get("ALERT_TO_EMAIL") or
                 "alex@fusionelectric-inc.com").strip()

    rows = fetch_pending()
    if not rows:
        print("no emails awaiting review; nothing to send")
        return

    body = render_digest(rows)
    svc = gmail_service()
    send_digest(svc, sender, recipient, body, len(rows))
    stamp_sent([r["id"] for r in rows])
    print(f"=== sent digest with {len(rows)} email(s) ===")


if __name__ == "__main__":
    main()
