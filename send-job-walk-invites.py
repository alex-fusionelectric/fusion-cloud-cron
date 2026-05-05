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
from email.mime.base import MIMEBase
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
KV_TABLE = "dropbox_kv_cloud"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # canonical scope; subsumes send
CLAUDE_API_KEY = (os.environ.get("CLAUDE_API_KEY") or "").strip()
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Default invite recipients always include Alex; the bid's PE is added when
# present. Estimator is excluded from job-walk invites per Alex 2026-05-05.
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
    # The PostgREST `in.()` value contains a space ("BID OR BAIL") that
    # Python 3.13's stricter URL validator rejects unless URL-encoded.
    # urllib.parse.quote with safe='()",' preserves the literal punctuation
    # PostgREST needs.
    in_clause = urllib.parse.quote('("BIDDING","BID OR BAIL")', safe='()",')
    qs = (f'status=in.{in_clause}&'
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


# --- Spec-doc location extraction ------------------------------------------

def fetch_cached_spec_text_for_est(est_number):
    """Pull every dropbox_kv_cloud row whose key path mentions this EST#
    (e.g. '/fusion electric folder/02- estimating/est# 25-396 .../...pdf@rev').
    The prequal scanner populated these. Returns concatenated text, capped."""
    if not est_number:
        return ""
    # PostgREST key like.* operator with URL-escaped wildcards. Use lower
    # case since path_lower is what gets stored.
    pattern = f"%est# {est_number.lower()}%"
    qs = f"key=ilike.{urllib.parse.quote(pattern)}&select=key,value&limit=20"
    status, body = _sb_request("GET", f"{KV_TABLE}?{qs}")
    if status != 200:
        return ""
    rows = json.loads(body)
    chunks = []
    for r in rows:
        v = r.get("value") or {}
        t = v.get("text") if isinstance(v, dict) else None
        if t:
            chunks.append(t)
    return "\n\n".join(chunks)[:100000]  # cap so we don't OOM


def extract_jobwalk_location(spec_text, *, project_name, fallback_location):
    """Send spec text near 'job walk' / 'pre-bid' mentions to Claude and
    ask for the meeting location. Returns a clean location string, or
    fallback_location if nothing useful comes back."""
    if not spec_text or not CLAUDE_API_KEY:
        return fallback_location
    # Pull paragraphs around any job-walk / pre-bid keyword. Most specs say
    # "Pre-bid conference will be held at [address]" or "Job walk to meet
    # at [room/door]".
    excerpts = []
    for m in re.finditer(r"[\s\S]{0,400}(?:job\s*walk|pre[-\s]?bid\s*conference|pre[-\s]?bid\s*meeting|mandatory\s*walk|site\s*visit)[\s\S]{0,400}",
                         spec_text, re.IGNORECASE):
        snippet = re.sub(r"\s+", " ", m.group(0)).strip()
        excerpts.append(snippet[:800])
        if len(excerpts) >= 6:
            break
    if not excerpts:
        return fallback_location
    blob = "\n\n---\n\n".join(excerpts)[:9000]
    prompt = f"""You are reading excerpts from a public-agency construction bid spec to find the job-walk meeting LOCATION.

Project: {project_name}
Excerpts:
{blob}

Return ONLY a JSON object (no prose, no markdown):
{{
  "location": "exact meeting address or building/room (e.g. '123 Main St, Hayward CA — Lobby of Bldg A'), or empty string if not stated"
}}
"""
    body_bytes = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body_bytes, method="POST",
        headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        print(f"  [llm-warn] {e}")
        return fallback_location
    text = ""
    for c in data.get("content", []):
        if c.get("type") == "text":
            text += c.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        loc = (json.loads(text).get("location") or "").strip()
    except Exception:  # noqa: BLE001
        return fallback_location
    return loc[:200] if loc else fallback_location


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

def ics_for_jobwalk(uid, *, project_name, est_number, start, location, organizer, description, attendees=None):
    # Gmail / Outlook hide the "Yes / Maybe / No" RSVP banner when the
    # ORGANIZER matches the recipient (you don't RSVP to your own meeting).
    # If the SMTP sender (alex@) is also a recipient, override the .ics
    # ORGANIZER to a service identity so Gmail treats this as an external
    # invite and renders the banner. The organizer email doesn't have to
    # be a real mailbox -- only the format is validated.
    SERVICE_ORGANIZER = "bidops@fusionelectric-inc.com"
    organizer_for_ics = SERVICE_ORGANIZER if (
        organizer and any(em == organizer for em in (attendees or []))
    ) else organizer
    end = start + dt.timedelta(hours=1)
    def esc(s):
        return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    def stamp(d):
        return d.strftime("%Y%m%dT%H%M%S")
    summary = f"Job Walk - EST# {est_number} {project_name}"
    now_utc = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    # Gmail (and Outlook) only render the "Yes / Maybe / No" calendar banner
    # at the top of the email when the recipient appears as an ATTENDEE in
    # the VEVENT. Without these lines the .ics arrives as a generic
    # attachment with no inline accept controls.
    attendee_lines = ""
    for em in (attendees or []):
        em = (em or "").strip()
        if not em or "@" not in em:
            continue
        attendee_lines += (
            f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN={em}:mailto:{em}\r\n"
        )
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
        "SEQUENCE:0\r\n"
        "STATUS:CONFIRMED\r\n"
        "TRANSP:OPAQUE\r\n"
        f"SUMMARY:{esc(summary)}\r\n"
        f"DESCRIPTION:{esc(description)}\r\n"
        f"LOCATION:{esc(location)}\r\n"
        f"ORGANIZER;CN=Fusion Bay PowerBid:mailto:{organizer_for_ics}\r\n"
        f"{attendee_lines}"
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


def _fold_ics(ics: str) -> str:
    """Fold ICS lines at 75 octets per RFC 5545 §3.1 (CRLF + single SPACE)."""
    out = []
    for line in ics.replace("\r\n", "\n").split("\n"):
        encoded = line.encode("utf-8")
        if len(encoded) <= 75:
            out.append(line + "\r\n")
            continue
        out.append(encoded[:75].decode("utf-8", "replace"))
        encoded = encoded[75:]
        while encoded:
            out.append("\r\n " + encoded[:74].decode("utf-8", "replace"))
            encoded = encoded[74:]
        out.append("\r\n")
    return "".join(out)


def _make_cal_part(ics_folded: str, disposition: str = "inline", filename: str | None = None) -> MIMEBase:
    """Build a text/calendar MIME part with method=REQUEST unquoted and no CTE.

    Uses MIMEBase + manual Content-Type header because MIMEText with
    charset='utf-8' (a) base64-encodes the payload and (b) quotes the method
    parameter as method="REQUEST" -- both of which prevent Gmail from
    rendering the RSVP banner.
    """
    part = MIMEBase("text", "calendar")
    del part["Content-Type"]
    ct = "text/calendar; method=REQUEST; charset=UTF-8"
    if filename:
        ct += f'; name="{filename}"'
    part["Content-Type"] = ct
    part["Content-Class"] = "urn:content-classes:calendarmessage"
    if filename:
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
    else:
        part["Content-Disposition"] = disposition
    part.set_payload(ics_folded)   # plain text payload — no base64 encoding
    return part


def build_invite_message(*, sender: str, to_list: list[str], subject: str,
                          body_text: str, ics_text: str, ics_filename: str) -> str:
    """Build and return the raw RFC-2822 MIME message bytes (base64url-encoded).

    MIME structure Gmail requires for the RSVP banner:

      multipart/mixed                           (outer)
        multipart/alternative                   (Gmail picks richest inline part)
          text/plain; charset=UTF-8
          text/calendar; method=REQUEST; charset=UTF-8   ← inline, no base64
        text/calendar; method=REQUEST; charset=UTF-8     ← attachment for download
          Content-Disposition: attachment; filename="jobwalk-NNN.ics"

    IMPORTANT — why the self-send RSVP banner doesn't appear:
      Gmail unconditionally suppresses the Yes/Maybe/No RSVP banner when the
      From: address matches the To: address (same Gmail account). This is a
      hard Gmail server-side rule that cannot be overridden via MIME headers,
      ICS ORGANIZER, or any other client-side technique. The invite IS
      delivered correctly; Gmail just doesn't render the banner for self-mail.
      To verify the banner works: send to a DIFFERENT Fusion email (jade@,
      gabriel.toler@, etc.) — those recipients will see the full RSVP widget.
    """
    ics_folded = _fold_ics(ics_text)

    # Inline calendar part — Gmail reads this to decide whether to show banner
    cal_inline = _make_cal_part(ics_folded, disposition="inline")

    # multipart/alternative: plain text + inline calendar
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain", "utf-8"))
    alt.attach(cal_inline)

    # text/calendar attachment — Outlook / Apple Calendar / Android download
    cal_attach = _make_cal_part(ics_folded, disposition="attachment", filename=ics_filename)

    # Outer multipart/mixed
    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg["Content-Class"] = "urn:content-classes:calendarmessage"
    msg.attach(alt)
    msg.attach(cal_attach)

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def send_invite(svc, *, sender, to_list, subject, body_text, ics_text, ics_filename,
                dump_mime: bool = False):
    raw = build_invite_message(
        sender=sender, to_list=to_list, subject=subject,
        body_text=body_text, ics_text=ics_text, ics_filename=ics_filename,
    )
    if dump_mime:
        import base64 as _b64
        print("=== RAW MIME (first 3000 chars) ===")
        print(_b64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")[:3000])
        print("=== END RAW MIME ===")
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
    skipped_past = 0
    for b in bids:
        payload = b.get("payload") or {}
        jw_raw = (payload.get("jobWalk") or "").strip()
        start = parse_jobwalk(jw_raw)
        if not start:
            continue
        # Skip past job walks. BID LIST stores wall-clock Pacific time.
        # The GitHub Actions runner is UTC, so dt.datetime.now() returns UTC.
        # Offset UTC by -7h (PDT) so we compare apples-to-apples with the
        # stored Pacific wall-clock time. Using -7 (PDT) is conservative --
        # any walk that hasn't started in Pacific time won't be skipped.
        now_pacific = dt.datetime.utcnow() - dt.timedelta(hours=7)
        if start < now_pacific:
            skipped_past += 1
            continue
        # Stable dedup id: est_number + sha1 of the JOB WALK string. Using
        # est_number (not bid.id) avoids the trap where bids_cloud's id
        # column gets a new value on every truncate-and-insert sync,
        # breaking the dedup. EST#s are stable. Re-scheduling a walk
        # generates a new hash and bypasses this dedup -- correct.
        h = hashlib.sha1(jw_raw.encode("utf-8")).hexdigest()[:12]
        est_for_dedup = (b.get("est_number") or b.get("id") or "?").strip()
        dedup_id = f"{est_for_dedup}:{h}"
        # Backwards-compat: also check against the legacy id format so we
        # don't re-send invites for walks that were ALREADY recorded under
        # the old (unstable) key shape during the rollout.
        legacy_dedup_id = f"{b.get('id', '')}:{h}"
        if dedup_id in already_sent or legacy_dedup_id in already_sent:
            continue

        # Recipients: Alex + assigned PE only.
        # Estimator is excluded from job-walk invites per Alex 2026-05-05.
        # Estimator still appears in the email body as project context.
        recipients = set(ALWAYS_INVITE)
        pe_name = b.get("project_engineer")
        if pe_name:
            pe_email = NAME_TO_EMAIL.get(str(pe_name).strip())
            if pe_email:
                recipients.add(pe_email)
        recipients = sorted(recipients)

        est = b.get("est_number") or "?"
        project = b.get("project_name") or "(untitled)"
        gc = b.get("client_gc") or ""
        docs = b.get("documents_url") or ""
        # Location precedence (highest to lowest):
        #   1. SBX listing's pre_bid_meeting_location (parsed from the
        #      "Pre Bid Conference: ... (LOCATION HERE) ..." block on the
        #      project page) -- this is the most reliable when present
        #   2. Claude extraction from cached spec docs
        #   3. payload.location from BID LIST
        #   4. "TBD" -- but never fall back to client_gc, because if the
        #      client got normalized to "MISC CUSTOMER" (not in KEYS list),
        #      using it as the meeting address makes no sense.
        sbx = b.get("sbx_listing") or {}
        sbx_pre_bid_loc = (sbx.get("pre_bid_meeting_location") or "").strip()
        bid_list_loc = (payload.get("location") or "").strip()
        if sbx_pre_bid_loc:
            location = sbx_pre_bid_loc
        else:
            spec_text = fetch_cached_spec_text_for_est(est)
            location = extract_jobwalk_location(
                spec_text, project_name=project,
                fallback_location=(bid_list_loc or "TBD"),
            )
        ics_uid = f"jobwalk-{b['id']}-{h}@fusionelectric-inc.com"
        # Pull richer bid context for the description summary
        bid_due = (payload.get("bidDueDate") or b.get("bid_due_date") or "").strip()
        bid_due_time = (payload.get("bidDueTime") or "").strip()
        bid_due_pretty = bid_due
        if bid_due_time and bid_due:
            bid_due_pretty = f"{bid_due} at {bid_due_time}"
        division = (b.get("division") or payload.get("division") or "").strip()
        scope = (payload.get("scope") or "").strip()
        # Format job walk into MM/DD/YYYY h:MM AM/PM (US convention)
        jw_pretty = start.strftime("%m/%d/%Y %I:%M %p").lstrip("0").replace(" 0", " ")
        description = "\n".join(filter(None, [
            "BID SUMMARY",
            "===========",
            f"EST #:        {est}",
            f"Project:      {project}",
            f"Client / GC:  {gc}" if gc else None,
            f"Division:     {division}" if division else None,
            f"Bid Due:      {bid_due_pretty}" if bid_due_pretty else None,
            f"Job Walk:     {jw_pretty}",
            f"Location:     {location}",
            f"Estimator:    {b.get('estimator') or '(unassigned)'}",
            f"Engineer:     {b.get('project_engineer') or '(unassigned)'}",
            f"Scope:        {scope[:200]}" if scope else None,
            f"Documents:    {docs}" if docs else None,
            "",
            "RSVP using the Yes / Maybe / No buttons in this email -- attendance lands on your Google Calendar.",
            "",
            "Auto-generated by Bay PowerBid from BID LIST 'JOB WALK' column.",
        ]))
        ics = ics_for_jobwalk(
            ics_uid,
            project_name=project, est_number=est, start=start,
            location=location, organizer=sender, description=description,
            attendees=recipients,
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

    print(f"\nSent {sent} new job-walk invite(s). Skipped {skipped_past} past-date walks.")


if __name__ == "__main__":
    main()
