"""cloud-sync-bid-invitations.py -- Cloud version of parse-gmail-invitations.py.
Walks Alex's Gmail inbox for likely bid invitations, runs Claude on each
to extract structured fields, writes to public.bid_invitations.

The Bay PowerBid 'Bid Radar' tab consumes that table.

Replaces the local heuristic classifier with Claude, since the training
pass over 1051 messages showed the heuristic missed ~60% of real invites
that BuildingConnected / SmartBid / direct-GC outreach sends with
non-standard subject lines.

GMAIL_QUERY widened from the training output's top sender domains:
buildingconnected, yourced, edgesgroup, sbayconstruction, main.inc, etc.
Existing classifications cached in public.gmail_kv_cloud (keyed by
"bidinv:{message_id}") so subsequent runs are nearly free.

Required env:
  GMAIL_TOKEN_JSON       OAuth (alex@fusionelectric-inc.com)
  CLAUDE_API_KEY
  SUPABASE_SERVICE_KEY

Optional env:
  BID_RADAR_LIMIT        cap candidate threads (default 200)
  BID_RADAR_DAYS         newer-than window (default 30)
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parseaddr
from pathlib import Path

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    from _claude_log import log_claude_call
except ImportError:
    def log_claude_call(**kwargs): pass  # no-op fallback

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
INVITATIONS_TABLE = "bid_invitations"
KV_TABLE = "gmail_kv_cloud"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # canonical scope; subsumes readonly
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Built from analyze-bid-invitations.py training output (1051 historical
# threads under ESTIMATING/SENT BIDS + CURRENT BIDS labels). The OR stew
# is unwieldy but it's the only way to get Gmail to return the right
# slice without a server-side label hierarchy.
GMAIL_QUERY = (
    "("
      # Bid platforms
      "from:buildingconnected.com OR "
      "from:planroom* OR from:bidexpress* OR from:smartbid* OR from:bxwa.com OR "
      "from:onlineplanservice* OR from:planhub* OR from:pipelinesuite* OR from:procore* OR "
      "from:planetbids* OR from:bidnet* OR from:isqft* OR "
      # Known direct-GC senders (add new domains as they appear)
      "from:yourced.com OR from:edgesgroup.com OR from:sbayconstruction.com OR "
      "from:envisioncdi.com OR from:zone4construction.com OR from:cumming-group.com OR "
      "from:tcgbuilders.com OR from:matrixhginc.com OR from:macom.com OR "
      "from:ironwoodcb.com OR "          # Ironwood Commercial Builders (LLNL bids)
      "from:gocc.smartbid.co OR "        # GSE Construction SmartBid portal
      "from:thecoregroup.com OR "        # The Core Group GC
      # Subject patterns — ITB is the most-missed abbreviation
      'subject:ITB OR subject:"invitation to bid" OR subject:"bid invitation" OR '
      'subject:"request for proposal" OR subject:"request for quote" OR '
      'subject:"request for quotation" OR subject:RFP OR subject:RFQ OR '
      'subject:"bid opportunity" OR subject:"prequalification" OR '
      'subject:"quote request" OR subject:"subcontractor bid" OR '
      # Looser patterns that pick up direct-GC outreach which doesn't use
      # ITB/RFP boilerplate (e.g. Marina Mechanical's "bid request 05/28").
      'subject:"bid request" OR subject:"request for bid" OR '
      'subject:"requesting quotes" OR subject:"electrical bid" OR '
      'subject:"electrical proposal" OR subject:"electrical quote"'
    ") "
    "-from:fusionelectric-inc.com "
    "-from:fusionelectricinc.onmicrosoft.com"
)

# Second query: scan the 00-POTENTIAL BIDS label with NO sender filter.
# Jake and the team forward bid invitations here (BuildingConnected, etc.).
# Because the From: shows a Fusion address (forwarder), the main GMAIL_QUERY
# excludes them. This separate label scan catches everything in that folder.
POTENTIAL_BIDS_LABEL_QUERY = "label:estimating-current-bids-00-potential-bids"

# Third query: catch forwards from internal Fusion addresses even when the
# email is NOT in the 00-POTENTIAL BIDS label. Pass 1's `-from:fusionelectric`
# exclusion drops anything Jade/Jake/etc. forwarded directly to Alex, so we
# query the internal-sender side separately and the body parser
# (parse_forwarded_block, below) re-attributes to the original external sender.
INTERNAL_FORWARDS_QUERY = (
    "(from:fusionelectric-inc.com OR from:fusionelectricinc.onmicrosoft.com) "
    '(subject:"Fwd" OR subject:"Fw:" OR subject:"FW:" OR '
    'subject:"bid" OR subject:"ITB" OR subject:"RFP" OR subject:"RFQ" OR '
    'subject:"invitation" OR subject:"request" OR subject:"addend" OR '
    'subject:"prequalification" OR subject:"plans" OR subject:"specs")'
)


# --- Forwarded-message parsing ---------------------------------------------
#
# When an internal Fusion user (e.g., Jade) forwards an external GC's bid
# invitation to Alex, the Gmail `From:` header shows the forwarder, not the
# original sender. We need to re-attribute so the classifier sees the real
# GC and Bid Radar shows correct sender_org.

FORWARD_MARKER_RE = re.compile(
    r"-{3,}\s*Forwarded message\s*-{3,}|^Begin forwarded message:",
    re.IGNORECASE | re.MULTILINE,
)
FORWARD_FROM_RE = re.compile(
    r"From:\s*(?P<name>[^<\n]*?)\s*<(?P<email>[^>\s]+@[^>\s]+)>",
    re.IGNORECASE,
)
FORWARD_SUBJ_RE = re.compile(r"^\s*Subject:\s*(?P<subj>[^\n]+)", re.IGNORECASE | re.MULTILINE)
FORWARD_DATE_RE = re.compile(r"^\s*Date:\s*(?P<date>[^\n]+)", re.IGNORECASE | re.MULTILINE)


def parse_forwarded_block(body: str) -> dict | None:
    """If `body` contains a forwarded-message block, extract the original
    sender's name + email + subject. Returns None if no marker is found
    or the From: line can't be parsed."""
    if not body:
        return None
    marker = FORWARD_MARKER_RE.search(body)
    if not marker:
        return None
    # Look at the ~1500 chars after the marker — that's where the forwarded
    # headers live. (Anything beyond is the original body.)
    after = body[marker.end():marker.end() + 1500]
    from_m = FORWARD_FROM_RE.search(after)
    if not from_m:
        return None
    name = (from_m.group("name") or "").strip().strip('"')
    email = (from_m.group("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    subj_m = FORWARD_SUBJ_RE.search(after)
    date_m = FORWARD_DATE_RE.search(after)
    return {
        "name":    name,
        "email":   email,
        "domain":  email.split("@", 1)[1] if "@" in email else "",
        "subject": (subj_m.group("subj").strip() if subj_m else None),
        "date":    (date_m.group("date").strip() if date_m else None),
        "header_line": f"{name} <{email}>".strip(),
    }


def detect_addenda(subject: str, body: str) -> dict:
    """Scan subject + body for addendum/addenda mentions. Returns
    {"count": N, "numbers": sorted_unique_ints}. count is the max of:
      - count of distinct numbered addenda found
      - 1 if any unnumbered "addendum"/"addenda" mention exists
    Used downstream to show a chip on the radar card and pre-populate
    bid_addenda_cloud at setup time."""
    text = f"{subject or ''}\n{body or ''}"
    nums: set[int] = set()
    any_mention = False
    for m in re.finditer(r"\baddend(?:um|a)\s*#?\s*(\d{1,3})?", text, re.IGNORECASE):
        any_mention = True
        n_str = m.group(1)
        if n_str:
            try: nums.add(int(n_str))
            except ValueError: pass
    if not any_mention:
        return {"count": 0, "numbers": []}
    return {
        "count":   max(len(nums), 1) if any_mention else 0,
        "numbers": sorted(nums),
    }


# --- Supabase REST helpers ---------------------------------------------------

def _service_key():
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb_request(method, path, *, body=None, headers_extra=None, timeout=30):
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


def kv_get_many(keys):
    if not keys:
        return {}
    out = {}
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        in_clause = ",".join(f'"{urllib.parse.quote(k, safe="")}"' for k in chunk)
        path = f"{KV_TABLE}?key=in.({in_clause})&select=key,value"
        status, body = _sb_request("GET", path)
        if status != 200:
            continue
        for r in json.loads(body):
            out[r["key"]] = r["value"]
    return out


def kv_upsert(key, value):
    body = [{"key": key, "value": value, "updated_at": dt.datetime.utcnow().isoformat() + "Z"}]
    status, _ = _sb_request(
        "POST", KV_TABLE, body=body,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    return status in (200, 201, 204)


def load_dismissed_ids():
    """Fetch IDs of rows already dismissed (is_invitation=false) so we don't
    resurrect them. Without this, the cron re-upserts dismissed rows with
    is_invitation=true and they reappear in Bid Radar after the user removed
    them."""
    status, body = _sb_request("GET", f"{INVITATIONS_TABLE}?select=id&is_invitation=eq.false")
    if status != 200:
        return set()
    try:
        return {r["id"] for r in json.loads(body)}
    except Exception:
        return set()


OPTIONAL_COLS = ("forwarded_by", "original_sender_name", "addenda_count", "addenda_numbers", "user_labeled")


def upsert_invitations(rows):
    if not rows:
        return 0
    dismissed = load_dismissed_ids()
    if dismissed:
        before = len(rows)
        rows = [r for r in rows if r["id"] not in dismissed]
        skipped = before - len(rows)
        if skipped:
            print(f"  [skip] {skipped} row(s) already user-dismissed (is_invitation=false)")
    if not rows:
        return 0
    status, resp = _sb_request(
        "POST", INVITATIONS_TABLE, body=rows,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    # If the migration for the new optional columns hasn't been run yet,
    # Supabase rejects the batch. Two known error shapes we retry on:
    #   - PGRST204 "Could not find the '<col>' column ... in the schema cache"
    #   - generic 42703 "column ... does not exist"
    # Strip the optional columns and retry so the existing core fields write.
    if status == 400 and isinstance(resp, (bytes, bytearray)):
        resp_txt = resp.decode("utf-8", errors="replace")
        col_missing = (
            "PGRST204" in resp_txt
            or "Could not find the" in resp_txt
            or "does not exist" in resp_txt
        )
        if col_missing and any(c in resp_txt for c in OPTIONAL_COLS):
            print(f"  [retry] optional columns missing — stripping {OPTIONAL_COLS} and retrying")
            stripped = [{k: v for k, v in r.items() if k not in OPTIONAL_COLS} for r in rows]
            status, resp = _sb_request(
                "POST", INVITATIONS_TABLE, body=stripped,
                headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
    if status not in (200, 201, 204):
        try:
            preview = (resp or b"").decode("utf-8", errors="replace")[:300]
        except Exception:
            preview = repr(resp)[:300]
        print(f"  [warn] upsert HTTP {status}: {preview!r}")
        return 0
    return len(rows)


# --- Gmail + body extraction ------------------------------------------------

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


def _decode(data):
    if not data: return ""
    try: return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception: return ""


def _strip_html(html):
    if not html: return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def extract_body(payload):
    if not payload: return ""
    parts = payload.get("parts") or []
    if not parts and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    plain, html = "", ""
    for p in parts:
        mt = p.get("mimeType", "")
        if mt.startswith("multipart/"):
            sub = extract_body(p)
            if sub and not plain: plain = sub
        elif mt == "text/plain" and not plain:
            plain = _decode(p.get("body", {}).get("data", ""))
        elif mt == "text/html" and not html:
            html = _decode(p.get("body", {}).get("data", ""))
    return plain if plain else _strip_html(html)


# --- Snippet builder --------------------------------------------------------

def _build_snippet(ext: dict) -> str | None:
    """Build a raw_snippet string from extracted fields so the frontend
    can detect job walk dates with a simple regex without needing a DB
    schema change for job_walk_date."""
    parts = []
    jw_date = ext.get("job_walk_date") or ""
    jw_time = ext.get("job_walk_time") or ""
    if jw_date:
        jw = f"Job Walk: {jw_date}"
        if jw_time:
            jw += f" {jw_time}"
        parts.append(jw)
    if ext.get("general_contractor"):
        parts.append(f"GC: {ext['general_contractor']}")
    if ext.get("owner"):
        parts.append(f"Owner: {ext['owner']}")
    return "  ".join(parts) if parts else None


# --- Claude classification --------------------------------------------------

def llm_extract(subject, body, sender, *, api_key):
    # Detect forwarded blocks. When an internal Fusion address forwards a
    # GC's bid request to Alex, the Gmail `From:` shows the forwarder and the
    # real sender is buried in the body. Re-attribute before classification.
    forwarded = parse_forwarded_block(body or "")
    forwarded_by = None
    classification_sender = sender
    classification_subject = subject
    sender_email = parseaddr(sender or "")[1].lower()
    if forwarded and (
        sender_email.endswith("@fusionelectric-inc.com")
        or sender_email.endswith("@fusionelectricinc.onmicrosoft.com")
    ):
        forwarded_by = sender
        classification_sender = forwarded["header_line"]
        if forwarded.get("subject"):
            classification_subject = forwarded["subject"]

    fwd_hint = ""
    if forwarded_by:
        fwd_hint = (
            "\nNOTE: This email was FORWARDED. The Gmail `From:` was an "
            "internal Fusion address; the line below shows the ORIGINAL "
            "external sender (use that for general_contractor)."
        )

    prompt = f"""Extract structured data from this email about whether it's a CONSTRUCTION BID INVITATION sent to an electrical contractor (Fusion Electric).

Return ONLY a JSON object (no prose, no markdown). Use null when unknown.

{{
  "is_invitation": true|false,
  "confidence": 0.0-1.0,
  "project_name": "short title" or null,
  "general_contractor": "GC org name" or null,
  "owner": "end-client / public agency" or null,
  "location": "city/state/area" or null,
  "bid_due_date": "YYYY-MM-DD" or null,
  "job_walk_date": "YYYY-MM-DD" or null,
  "job_walk_time": "HH:MM AM/PM" or null,
  "scope_hints": ["distribution","fire alarm","low voltage","lighting","security","audio visual","nurse call","trenching"] subset,
  "estimating_platform": "BuildingConnected|SmartBid|PipelineSuite|PlanHub|BidNet|PlanRoom|PlanetBids|Procore|iSqFt|direct-email|other" or null,
  "permalink": "URL to the bid portal/listing" or null
}}

Notes for `bid_due_date`:
- Look for labeled patterns first: "Bid Due Date:", "Bid Date:", "Due:",
  "Proposals Due:", "Submit By:", "Quote Due:". These are authoritative.
- Match relative phrases like "bid request 05/28" or "due 6/1" too —
  the year is the next future year if month/day already passed in current year.
- Return null if no date is stated; do NOT guess from the received date.
{fwd_hint}

EMAIL:
From: {classification_sender}
Subject: {classification_subject}
Body (first 4000 chars):
{(body or '')[:4000]}
"""
    body_bytes = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body_bytes, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
        log_claude_call(feature='bid-invitations-classify', model=data.get('model') or 'unknown', usage=data.get('usage'))
    except urllib.error.HTTPError as e:
        body_txt = ""
        try: body_txt = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        print(f"  [llm-warn] HTTP {e.code}: {body_txt}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  [llm-warn] {e}")
        return None
    text = ""
    for c in data.get("content", []):
        if c.get("type") == "text":
            text += c.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    # Stamp forwarded metadata so the row builder + frontend can use it.
    if forwarded_by:
        parsed["forwarded_by"] = forwarded_by
        parsed["original_sender_email"] = forwarded.get("email")
        parsed["original_sender_name"] = forwarded.get("name")
        parsed["original_sender_domain"] = forwarded.get("domain")
    # Stamp addenda count + numbers so the radar can show a chip and
    # downstream cron (detect-bid-addenda) can pre-seed bid_addenda_cloud.
    addenda = detect_addenda(subject, body)
    if addenda["count"] > 0:
        parsed["addenda_count"] = addenda["count"]
        parsed["addenda_numbers"] = addenda["numbers"]
    return parsed


# --- Main -------------------------------------------------------------------

def main():
    api_key = (os.environ.get("CLAUDE_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("CLAUDE_API_KEY env var required.")
    days = int(os.environ.get("BID_RADAR_DAYS") or 30)
    cap = int(os.environ.get("BID_RADAR_LIMIT") or 200)

    svc = gmail_service()

    def _fetch_candidates(q: str, limit: int) -> list[dict]:
        out, page_token = [], None
        while True:
            kwargs = {"userId": "me", "q": q, "maxResults": min(100, limit - len(out))}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = svc.users().messages().list(**kwargs).execute()
            out.extend(resp.get("messages", []) or [])
            page_token = resp.get("nextPageToken")
            if not page_token or len(out) >= limit:
                break
        return out

    # Pass 1: external-sender query (GC platforms, known GC domains, keywords)
    query = f"{GMAIL_QUERY} newer_than:{days}d"
    print(f"Gmail query pass 1 (cap {cap}): {query[:120]}...")
    candidates = _fetch_candidates(query, cap)
    for m in candidates:
        m["_source_pass"] = "external_sender"
    print(f"  Pass 1: {len(candidates)} messages")

    # Pass 2: 00-POTENTIAL BIDS label (no sender filter — Jake/team forward bids
    # here from BuildingConnected etc.; the From: shows Fusion so pass 1 misses them)
    label_query = f"{POTENTIAL_BIDS_LABEL_QUERY} newer_than:{days}d"
    label_candidates = _fetch_candidates(label_query, 200)
    # Track every thread that the human placed in the label, even if it
    # was also found by Pass 1. user_labeled wins on conflict because
    # the human signal is authoritative.
    labeled_ids = {m["id"] for m in label_candidates}
    for m in candidates:
        if m["id"] in labeled_ids:
            m["_source_pass"] = "label_scan"
    seen_ids = {m["id"] for m in candidates}
    new_from_label = [m for m in label_candidates if m["id"] not in seen_ids]
    for m in new_from_label:
        m["_source_pass"] = "label_scan"
    candidates.extend(new_from_label)
    print(f"  Pass 2 (00-POTENTIAL BIDS label): {len(label_candidates)} found, {len(new_from_label)} new")

    # Pass 3: internal-forwarded emails. Catches bid invitations forwarded by
    # Jade/Jake/etc. to Alex that aren't in the 00-POTENTIAL BIDS label.
    # Subject keyword filter avoids dragging the full internal inbox; body
    # forward-marker check happens during classification (parse_forwarded_block).
    internal_window = min(days, 14)  # internal volume is high; tighten window
    internal_query = f"{INTERNAL_FORWARDS_QUERY} newer_than:{internal_window}d"
    internal_candidates = _fetch_candidates(internal_query, 150)
    seen_ids.update(m["id"] for m in candidates)
    new_from_internal = [m for m in internal_candidates if m["id"] not in seen_ids]
    for m in new_from_internal:
        m["_source_pass"] = "internal_forward"
    candidates.extend(new_from_internal)
    print(f"  Pass 3 (internal forwards): {len(internal_candidates)} found, {len(new_from_internal)} new")
    print(f"Total candidates after all passes: {len(candidates)}")

    cache_keys = [f"bidinv:{m['id']}" for m in candidates]
    cache = kv_get_many(cache_keys)
    print(f"Cache hits: {len(cache)} of {len(candidates)}")

    today = dt.date.today()
    rows = []
    classified = 0
    skipped = 0
    fetch_404 = 0
    fetch_other_err = 0
    for i, m in enumerate(candidates, 1):
        ck = f"bidinv:{m['id']}"
        ext = cache.get(ck)
        if not ext or not isinstance(ext, dict) or "is_invitation" not in ext:
            try:
                full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            except Exception as fetch_err:  # noqa: BLE001
                # 404 = message deleted between list and get, or moved to a
                # label the OAuth scope can't read. Skip it; don't abort the run.
                msg = str(fetch_err)
                if "404" in msg or "notFound" in msg:
                    fetch_404 += 1
                else:
                    fetch_other_err += 1
                    print(f"  [fetch-warn] {m['id']}: {msg[:200]}")
                skipped += 1
                continue
            headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            body = extract_body(full.get("payload", {}))

            # Cheap pre-Claude filters. These exist because Pass 3 widens
            # the candidate net to ALL internal-Fusion senders, which
            # drags in bid-setup-complete notifications, daily digests,
            # vendor replies, etc. None of those are real invitations,
            # and Claude can occasionally be tricked by them.
            #
            # EXCEPTION: candidates from Pass 2 (00-POTENTIAL BIDS label)
            # bypass these filters entirely — a human deliberately put
            # them there, so we honor the label as authoritative.
            user_labeled = (m.get("_source_pass") == "label_scan")
            sender_email_lc = parseaddr(sender)[1].lower()
            sender_is_internal = (
                sender_email_lc.endswith("@fusionelectric-inc.com")
                or sender_email_lc.endswith("@fusionelectricinc.onmicrosoft.com")
            )
            has_forward = parse_forwarded_block(body) is not None
            # EST# marker — labeled form ("EST# 26-275") is the canonical
            # Fusion notification footprint; loose form (\d{2}-\d{3,4})
            # catches vendor replies that quote "26-275" in the subject.
            EST_LABELED_RX = re.compile(r"\bEST#?\s*\d{2}-\d{3,4}\b", re.IGNORECASE)
            EST_LOOSE_RX   = re.compile(r"\b\d{2}-\d{3,4}\b")
            skip_reason = None
            if not user_labeled:
                if sender_is_internal and not has_forward:
                    skip_reason = "internal_no_forward"
                elif EST_LABELED_RX.search(subject) or EST_LABELED_RX.search(body or ""):
                    skip_reason = "has_est_label"
                elif EST_LOOSE_RX.search(subject):  # subject only — body matches too aggressive
                    skip_reason = "est_in_subject"

            if skip_reason:
                ext = {"is_invitation": False, "reason": skip_reason}
                kv_upsert(ck, ext)
                # No sleep — we didn't hit Claude
                continue

            ext = llm_extract(subject, body, sender, api_key=api_key)
            classified += 1
            if ext is None:
                skipped += 1
                continue
            # User-labeled rows: trust the human signal. Claude still
            # extracts structured fields (due date, GC, location, etc.)
            # but we override is_invitation=True so the radar shows it.
            if user_labeled:
                ext["is_invitation"] = True
                ext["user_labeled"] = True
            kv_upsert(ck, ext)
            time.sleep(0.2)
        # If this candidate is user-labeled (Pass 2) but the cache has
        # a stale not-invitation verdict from a prior run, override at
        # runtime so the human label always wins without forcing a
        # full re-classification.
        if (m.get("_source_pass") == "label_scan") and not ext.get("is_invitation"):
            ext["is_invitation"] = True
            ext["user_labeled"] = True
        if not ext.get("is_invitation"):
            continue

        # Skip out-of-state invitations. Fusion Electric bids only in
        # California. If the extracted location explicitly names another
        # US state, it's not a bid we'd pursue. Per Alex 2026-05-04:
        # "maybe not suggest out of state also".
        _loc = (ext.get("location") or "").upper()
        _OUT_OF_STATE = re.compile(
            r"\b(ID|TX|OR|WA|AZ|UT|CO|NM|MT|WY|FL|NY|GA|IL|OH|PA|NC|"
            r"VA|MA|MD|MI|MN|NV|HI|AK|IN|WI|MO|TN|AL|SC|KY|OK|AR|IA|"
            r"KS|MS|NE|SD|ND|VT|NH|ME|DE|RI|WV|DC)\b"
        )
        # Whitelist: Claude occasionally hallucinates a non-CA state code
        # next to a clearly-CA city ("San Francisco, VA"). If the location
        # contains any of these CA cities, override the out-of-state guard.
        _CA_CITIES = (
            "SAN FRANCISCO", "OAKLAND", "SAN JOSE", "SACRAMENTO", "FREMONT",
            "BERKELEY", "PALO ALTO", "REDWOOD CITY", "MOUNTAIN VIEW", "HAYWARD",
            "SAN MATEO", "SAN RAFAEL", "SAN BRUNO", "SAN LEANDRO", "DALY CITY",
            "SOUTH SAN FRANCISCO", "SANTA CLARA", "SUNNYVALE", "MILPITAS",
            "GILROY", "MORGAN HILL", "PLEASANTON", "DUBLIN", "LIVERMORE",
            "ANTIOCH", "CONCORD", "WALNUT CREEK", "MARTINEZ", "PITTSBURG",
            "BRENTWOOD", "TRACY", "MANTECA", "MODESTO", "STOCKTON", "FRESNO",
            "BAKERSFIELD", "LOS ANGELES", "LONG BEACH", "PASADENA", "BURBANK",
            "GLENDALE", "SAN DIEGO", "ANAHEIM", "SANTA ANA", "IRVINE",
            "RIVERSIDE", "SAN BERNARDINO", "SANTA ROSA", "NAPA", "VALLEJO",
            "FAIRFIELD", "VACAVILLE", "DAVIS", "WOODLAND", "REDDING", "CHICO",
            "MONTEREY", "SALINAS", "SANTA CRUZ", "WATSONVILLE",
            "RANCHO CORDOVA", "ELK GROVE", "ROSEVILLE", "ROCKLIN", "FOLSOM",
            "CITRUS HEIGHTS", "MERCED", "TURLOCK", "VISALIA",
            "OCEANSIDE", "CARLSBAD", "ESCONDIDO", "CHULA VISTA",
            "TURLOCK", "CUPERTINO", "LOS GATOS", "CAMPBELL", "SARATOGA",
            "NEWARK", "UNION CITY", "RICHMOND", "EL CERRITO", "SAN RAMON",
            "DANVILLE", "ORINDA", "LAFAYETTE", "MORAGA", "ALAMEDA",
            "EMERYVILLE", "PIEDMONT", "ALBANY", "PINOLE", "HERCULES",
        )
        _ca_explicit = (
            "CA" in _loc.split()  # word-boundary match for "CA"
            or "CALIFORNIA" in _loc
            or any(c in _loc for c in _CA_CITIES)
        )
        # User-labeled rows bypass the out-of-state filter too — if a
        # human dragged it into 00-POTENTIAL BIDS, they wanted it shown.
        _user_labeled_row = ext.get("user_labeled") or (m.get("_source_pass") == "label_scan")
        if _loc and _OUT_OF_STATE.search(_loc) and not _ca_explicit and not _user_labeled_row:
            print(f"  [skip] out-of-state location: {ext.get('location')!r}")
            continue

        # Skip past-due invitations -- the Bay PowerBid Bid Radar tab is
        # forward-looking; archived ones live in the original Sent labels.
        bid_due = ext.get("bid_due_date")
        if bid_due:
            try:
                if dt.date.fromisoformat(bid_due) < today:
                    continue
            except Exception:
                pass

        # We need received_at for the table row. Fetch only if we used cache
        # (skipped the full Gmail get above).
        if "received_at" not in ext:
            try:
                meta = svc.users().messages().get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From","Subject","Date"]).execute()
            except Exception as meta_err:  # noqa: BLE001
                if "404" in str(meta_err) or "notFound" in str(meta_err):
                    fetch_404 += 1
                else:
                    fetch_other_err += 1
                    print(f"  [meta-fetch-warn] {m['id']}: {str(meta_err)[:200]}")
                continue
            mh = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
            received_ms = int(meta.get("internalDate", 0))
            received_iso = dt.datetime.utcfromtimestamp(received_ms / 1000).isoformat() + "Z" if received_ms else None
            sender = mh.get("from", "")
            subject = mh.get("subject", "")
        else:
            received_iso = ext.get("received_at")
            sender = ext.get("sender") or ""
            subject = ext.get("subject") or ""

        sender_addr = parseaddr(sender)[1].lower()
        sender_org = sender_addr.split("@", 1)[1] if "@" in sender_addr else ""

        # If the classifier detected a forward, the real GC is the ORIGINAL
        # external sender extracted from the forwarded block, not the Fusion
        # forwarder. Re-attribute sender / sender_org for the row.
        forwarded_by = ext.get("forwarded_by")
        if forwarded_by and ext.get("original_sender_email"):
            sender_addr = ext.get("original_sender_email")
            sender_org = (
                ext.get("original_sender_domain")
                or (sender_addr.split("@", 1)[1] if "@" in sender_addr else "")
            )

        stable_id = hashlib.sha1(m["id"].encode("utf-8")).hexdigest()[:24]
        # Always include every column so PostgREST batch upsert sees a
        # uniform shape (it returns PGRST102 "All object keys must match"
        # otherwise). New optional cols default to null / 0 / [].
        row = {
            "id":              stable_id,
            "thread_id":       m.get("threadId"),
            "message_id":      m["id"],
            "subject":         (ext.get("project_name") or subject)[:500],
            "sender":          sender_addr,
            "sender_org":      sender_org,
            "project_name":    (ext.get("project_name") or "")[:300],
            "project_location": ext.get("location"),
            "bid_due_date":    bid_due,
            "scope_summary":   ", ".join(ext.get("scope_hints") or []) or None,
            "scope_codes":     ext.get("scope_hints") or [],
            "is_invitation":   True,
            "confidence":      float(ext.get("confidence") or 0.5),
            "permalink":       ext.get("permalink"),
            # raw_snippet stores job walk info so the frontend can display it
            "raw_snippet":     _build_snippet(ext),
            "received_at":     received_iso,
            "generated_at":    dt.datetime.utcnow().isoformat() + "Z",
            "updated_at":      dt.datetime.utcnow().isoformat() + "Z",
            # Forwarded-message + addenda metadata. Always included (nullable)
            # to keep the batch shape uniform. Columns added by
            # fusion-bid-list/sql/bid_invitations_forward_addenda_cols.sql;
            # upsert_invitations() retries without these if the migration
            # hasn't been run.
            "forwarded_by":         (forwarded_by or "")[:300] or None,
            "original_sender_name": (ext.get("original_sender_name") or "")[:200] or None,
            "addenda_count":        int(ext.get("addenda_count") or 0),
            "addenda_numbers":      ext.get("addenda_numbers") or [],
            # user_labeled = TRUE when found via Pass 2 (00-POTENTIAL BIDS
            # label scan). The radar + Gmail extension treat this as
            # authoritative and skip heuristic filters. Column added by
            # bid_invitations_user_labeled_col.sql.
            "user_labeled":         bool(_user_labeled_row),
        }
        rows.append(row)

    print(f"\nClassified {classified} new (cache misses), {skipped} skipped "
          f"({fetch_404} fetch 404s, {fetch_other_err} other fetch errs, rest = LLM failures).")
    print(f"Future-dated invitations to upsert: {len(rows)}")
    n = upsert_invitations(rows)
    print(f"Wrote {n} row(s) to {INVITATIONS_TABLE}.")


if __name__ == "__main__":
    main()
