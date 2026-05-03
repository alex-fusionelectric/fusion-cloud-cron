"""send-job-walk-invites.py -- Walks active bids in public.bids_cloud,
finds ones with a JOB WALK datetime that hasn't been emailed yet, and
sends a calendar invite (.ics attachment) via Gmail to Alex and the
assigned Estimator/PE. Tracks dedup in public.job_walk_invites_sent_cloud
so a re-run doesn't double-send. If JOB WALK is rescheduled, the changed
string mints a new dedup id and a fresh invite goes out.

Required env:
  GMAIL_TOKEN_JSON       -- OAuth (same one parse-gmail-quotes uses)
  SUPABASE_SERVICE_KEY
  GMAIL_FROM             -- e.g. alex@fusionelectric-inc.com
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
BIDS_TABLE = "bids_cloud"
SENT_TABLE = "job_walk_invites_sent_cloud"
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Default invite recipients always include Alex; the bid's estimator + PE
# get added when present.
ALWAYS_INVITE = {"alex@fusionelectric-inc.com"}

# Map estimator/PE name -> email so we can copy them on the invite. The
# BID LIST stores name strings; we map to emails here. Keep synced with
# the personnel dropdown on the SBX Set-up-Bid modal.
NAME_TO_EMAIL = {
    "Alex Toler":     "alex@fusionelectric-inc.com",
    "Jade Sueki":     "jade@fusionelectric-inc.com",
    "Jake Duenkel":   "jake@fusionelectric-inc.com",
    "Gabriel Toler":  "gabriel.toler@fusionelectric-inc.com",
    "Austin Carmichael": "austin@fusionelectric-inc.com",
    "Christine":      "christine@fusionelectric-inc.com",
}


# --- Supabase REST helpers ---------------------------------------------------

def _service_key():
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb_request(method, path, body=None, headers_extra=None, timeout=30):
    key = _service_key()
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "content-type": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def fetch_active_bids():
    qs = ('status=in.("BIDDING","BID OR BAIL")&'
          'select=id,est_number,project_name,client_gc,estimator,project_engineer,'
          'documents_url,division,payload')
    status, body = _sb_request("GET", f"{BIDS_TABLE}?{qs}")
    if status != 200:
        raise SystemExit(f"bids_cloud GET failed: HTTP {status} {body[:200]!r}")
    return json.loads(body)


def fetch_already_sent_ids():
    status, body = _sb_request("GET", f"{SENT_TABLE}?select=id")
    if status != 200:
        print(f"[warn] {SENT_TABLE} GET failed: HTTP {status} -- treating as empty.")
        return set()
    return {r["id"] for r in json.loads(body)}


def record_sent(row):
    status, body = _sb_request(
        "POST", SENT_TABLE, body=[row],
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if status not in (200, 201, 204):
        print(f"[warn] sent insert failed: HTTP {status} {body[:200]!r}")


# --- Parse JOB WALK string --------------------------------------------------

JOBWALK_FORMATS = [
    "%m/%d/%y %I:%M %p",  # 4/7/26 2:00 PM
    "%m/%d/%Y %I:%M %p",  # 4/7/2026 2:00 PM
    "%m/%d/%y %H:%M",     # 4/7/26 14:00
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def parse_jobwalk(s):
    s = (s or "").strip()
    if not s or re.match(r"^(n/?a|none|tbd|tba|no walk)\b", s, re.IGNORECASE):
        return None
    for fmt in JOBWALK_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --- Build .ics --------------------------------------------------------------

def ics_for_jobwalk(uid, *, project_name, est_number, start, location, organizer, description):
    end = start + dt.timedelta(hours=1)
    def esc(s):
        return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    def stamp(d):
        return d.strftime("%Y%m%dT%H%M%S")
    summary = f"Job Walk - EST# {est_number} {project_name}"
    now_utc = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Fusion Electric//Bay PowerBid Job Walk//EN\r\n"
        "METHOD:REQUEST\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now_utc}\r\n"
        # Floating times -- BID LIST stores wall-clock, no timezone. Outlook/
        # Gmail show them in the user's local TZ which matches Fusion's PT.
        f"DTSTART:{stamp(start)}\r\n"
        f"DTEND:{stamp(end)}\r\n"
        f"SUMMARY:{esc(summary)}\r\n"
        f"DESCRIPTION:{esc(description)}\r\n"
        f"LOCATION:{esc(location)}\r\n"
        f"ORGANIZER;CN=Fusion Electric:mailto:{organizer}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


# --- Gmail send -------------------------------------------------------------

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


def send_invite(svc, *, sender, to_list, subject, body_text, ics_text, ics_filename):
    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    cal = MIMEText(ics_text, "calendar; method=REQUEST", "utf-8")
    cal.add_header("Content-Class", "urn:content-classes:calendarmessage")
    msg.attach(cal)
    ics_attach = MIMEApplication(ics_text.encode("utf-8"), _subtype="ics")
    ics_attach.add_header("Content-Disposition", "attachment", filename=ics_filename)
    msg.attach(ics_attach)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


# --- Main -------------------------------------------------------------------

def main():
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender:
        raise SystemExit("GMAIL_FROM env var required.")

    bids = fetch_active_bids()
    already_sent = fetch_already_sent_ids()
    print(f"Active bids: {len(bids)}; already-sent invite ids: {len(already_sent)}")

    svc = gmail_service()
    sent = 0
    for b in bids:
        payload = b.get("payload") or {}
        jw_raw = (payload.get("jobWalk") or "").strip()
        start = parse_jobwalk(jw_raw)
        if not start:
            continue
        # Stable id for the dedup table -- bid_id + first 12 chars of sha of
        # the JOB WALK string. Re-scheduling produces a new id automatically.
        h = hashlib.sha1(jw_raw.encode("utf-8")).hexdigest()[:12]
        dedup_id = f"{b['id']}:{h}"
        if dedup_id in already_sent:
            continue

        # Recipients: Alex + assigned estimator + PE (when we know their email).
        recipients = set(ALWAYS_INVITE)
        for nm in (b.get("estimator"), b.get("project_engineer")):
            if not nm:
                continue
            email = NAME_TO_EMAIL.get(str(nm).strip())
            if email:
                recipients.add(email)
        recipients = sorted(recipients)

        est = b.get("est_number") or "?"
        project = b.get("project_name") or "(untitled)"
        gc = b.get("client_gc") or ""
        docs = b.get("documents_url") or ""
        location = (payload.get("location") or "").strip() or gc or "TBD"
        ics_uid = f"jobwalk-{b['id']}-{h}@fusionelectric-inc.com"
        description = "\n".join(filter(None, [
            f"Project: EST# {est} {project}",
            f"Client / GC: {gc}" if gc else None,
            f"Estimator: {b.get('estimator') or '(unassigned)'}",
            f"Project Engineer: {b.get('project_engineer') or '(unassigned)'}",
            f"Documents: {docs}" if docs else None,
            "",
            "Auto-generated by Bay PowerBid from BID LIST 'JOB WALK' column.",
        ]))
        ics = ics_for_jobwalk(
            ics_uid,
            project_name=project, est_number=est, start=start,
            location=location, organizer=sender, description=description,
        )
        subject = f"Job Walk: EST# {est} {project} - {start.strftime('%a %b %d, %I:%M %p')}"
        body_text = description + "\n\nThis email contains a calendar invite (.ics attachment)."

        try:
            send_invite(svc, sender=sender, to_list=recipients,
                        subject=subject, body_text=body_text,
                        ics_text=ics, ics_filename=f"jobwalk-{est}.ics")
            sent += 1
            print(f"  sent invite for EST# {est} {project[:40]}  walk={jw_raw}  -> {recipients}")
            record_sent({
                "id":           dedup_id,
                "bid_id":       b["id"],
                "est_number":   est,
                "project_name": project,
                "job_walk":     jw_raw,
                "job_walk_iso": start.isoformat(),
                "recipients":   recipients,
                "ics_uid":      ics_uid,
                "sent_at":      dt.datetime.utcnow().isoformat() + "Z",
            })
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] send failed for EST# {est}: {e}")

    print(f"\nSent {sent} new job-walk invite(s).")


if __name__ == "__main__":
    main()
