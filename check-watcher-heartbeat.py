"""check-watcher-heartbeat.py -- Monitors the bid setup watcher's
heartbeat (in local_watcher_status_cloud) and emails Alex on transitions
between online and offline.

Runs every 5 min from GitHub Actions. The watcher writes a heartbeat each
poll cycle (~60s); if no heartbeat for OFFLINE_THRESHOLD_MIN minutes, we
flip alert state to 'offline' and email. When heartbeats resume, flip
back to 'online' and email the all-clear.

Single source of truth for state: watcher_alert_state_cloud (singleton
row, id='main'). The cron updates this row exactly when the state
actually changes, so we never double-email.

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
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
HEARTBEAT_TABLE = "local_watcher_status_cloud"
ALERT_TABLE     = "watcher_alert_state_cloud"
WATCHER_ID      = "main"
SCOPES          = ["https://www.googleapis.com/auth/gmail.send"]

# Heartbeat older than this -> offline. Watcher polls every 60s; allow
# generous buffer for transient stalls (Excel autofill can take ~3 min).
OFFLINE_THRESHOLD_MIN = 15


def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb(method: str, path: str, body=None, extra=None, timeout=30):
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


def fetch_latest_heartbeat() -> dict | None:
    qs = f"select=*&watcher_id=eq.{WATCHER_ID}&limit=1"
    st, body = _sb("GET", f"{HEARTBEAT_TABLE}?{qs}")
    if st != 200:
        print(f"[warn] heartbeat GET failed: HTTP {st} {body[:200]!r}",
              file=sys.stderr)
        return None
    rows = json.loads(body)
    return rows[0] if rows else None


def fetch_alert_state() -> dict | None:
    qs = f"select=*&id=eq.{WATCHER_ID}&limit=1"
    st, body = _sb("GET", f"{ALERT_TABLE}?{qs}")
    if st != 200:
        print(f"[warn] alert state GET failed: HTTP {st} {body[:200]!r}",
              file=sys.stderr)
        return None
    rows = json.loads(body)
    return rows[0] if rows else None


def update_alert_state(new_state: str, sent_email: bool, note: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {"state": new_state, "updated_at": now_iso, "notes": note,
            "last_change_at": now_iso}
    if sent_email:
        body["last_alert_at"] = now_iso
    st, resp = _sb(
        "PATCH",
        f"{ALERT_TABLE}?id=eq.{urllib.parse.quote(WATCHER_ID, safe='')}",
        body=body,
    )
    if st not in (200, 204):
        print(f"[warn] alert state PATCH failed: HTTP {st} {resp[:200]!r}",
              file=sys.stderr)


def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fmt_age(seconds: int) -> str:
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400: return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def send_alert(svc, sender: str, recipient: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[ok] alert sent: {subject}")


def build_offline_email(hb: dict, last_change_at: str | None,
                        age_sec: int) -> tuple[str, str]:
    host    = hb.get("hostname") or "watcher PC"
    pending = hb.get("pending_count") or 0
    last_iso = hb.get("last_heartbeat_at") or "?"
    subj = f"[Fusion Bid Watcher] OFFLINE -- no heartbeat in {fmt_age(age_sec)}"
    body = (
        f"The bid setup watcher on {host} has stopped sending heartbeats.\n\n"
        f"Last heartbeat:  {last_iso} ({fmt_age(age_sec)} ago)\n"
        f"Threshold:       {OFFLINE_THRESHOLD_MIN} min\n"
        f"Pending bids:    {pending} (queued, will process when watcher is back)\n\n"
        f"What this means:\n"
        f"  - New 'Setup Bid' clicks WILL queue in Supabase, but won't be processed\n"
        f"    (no folder copy, no plans/specs download, no BID BREAKDOWN autofill)\n"
        f"    until the watcher is alive again.\n\n"
        f"What to check:\n"
        f"  1. Is your PC ({host}) on and signed in?\n"
        f"  2. Is the watcher running? Look for the colored dot in your system tray.\n"
        f"  3. If tray icon is missing, restart via the Start Bid Watcher.bat\n"
        f"     desktop shortcut OR run Start-ScheduledTask -TaskName 'Fusion Bid Setup Watcher'.\n"
        f"  4. Check ~\\.fusion-bid-watcher.log for any startup errors.\n\n"
        f"You'll get a 'BACK ONLINE' email once heartbeats resume.\n"
    )
    return subj, body


def build_recovery_email(hb: dict, offline_for_sec: int) -> tuple[str, str]:
    host    = hb.get("hostname") or "watcher PC"
    pending = hb.get("pending_count") or 0
    last_iso = hb.get("last_heartbeat_at") or "?"
    subj = f"[Fusion Bid Watcher] BACK ONLINE -- heartbeats resumed"
    body = (
        f"Watcher on {host} is alive again.\n\n"
        f"Resumed:        {last_iso}\n"
        f"Was offline:    {fmt_age(offline_for_sec)}\n"
        f"Pending bids:   {pending} (will be processed in the next poll)\n\n"
        f"No action needed. Watcher will drain the queue automatically.\n"
    )
    return subj, body


def main():
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender:
        raise SystemExit("GMAIL_FROM env var required.")
    recipient = (os.environ.get("ALERT_TO_EMAIL") or
                 "alex@fusionelectric-inc.com").strip()

    hb = fetch_latest_heartbeat()
    if hb is None or not hb.get("last_heartbeat_at"):
        # No heartbeat row at all (table empty). Treat as 'unknown' state
        # to avoid waking Alex up over a never-started watcher. We'll only
        # alert once we've seen at least one heartbeat in the past.
        print("[info] no heartbeat row yet -- nothing to compare; exiting.")
        return

    now_utc = datetime.now(timezone.utc)
    last_iso = hb["last_heartbeat_at"]
    last_dt  = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    age_sec  = max(0, int((now_utc - last_dt).total_seconds()))

    new_state = "offline" if age_sec >= OFFLINE_THRESHOLD_MIN * 60 else "online"

    alert = fetch_alert_state()
    if alert is None:
        # Table missing -- run the SQL migration and bail. Don't email until
        # we have a state record to track transitions, otherwise we'd email
        # every cron run.
        print(f"[warn] {ALERT_TABLE} not found. Run watcher-alert-state-table.sql"
              f" in Supabase SQL editor, then this cron starts working.")
        return
    prev_state = alert.get("state") or "unknown"
    prev_change_at = alert.get("last_change_at")

    print(f"[debug] heartbeat age={age_sec}s  new_state={new_state}  "
          f"prev_state={prev_state}")

    if new_state == prev_state:
        # No transition -- silent.
        return

    # State changed -- email + persist.
    svc = gmail_service()
    if new_state == "offline":
        subj, body = build_offline_email(hb, prev_change_at, age_sec)
    else:
        # Going online. Compute how long it was offline.
        if prev_change_at:
            try:
                pdt = datetime.fromisoformat(prev_change_at.replace("Z", "+00:00"))
                offline_for = max(0, int((now_utc - pdt).total_seconds()))
            except Exception:
                offline_for = 0
        else:
            offline_for = 0
        subj, body = build_recovery_email(hb, offline_for)

    try:
        send_alert(svc, sender, recipient, subj, body)
        update_alert_state(new_state, sent_email=True,
                           note=f"transition {prev_state} -> {new_state}; "
                                f"hb age {age_sec}s")
    except Exception as exc:
        # Failed to send -- DON'T flip state in the DB; we want the next
        # cron run to retry the email rather than silently swallowing the
        # missed alert.
        print(f"[error] failed to send alert email: {exc}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
