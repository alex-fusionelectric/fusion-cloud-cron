"""parse-prequal-emails.py -- Cloud-side parser for Kim Dias's
prequalification approval / renewal / expiration emails. Walks Alex's
inbox for messages from kim@fusionelectric-inc.com that look like prequal
notifications, extracts structured fields via Claude, writes to
public.prequal_approvals_cloud.

The Bay Bid List Prequal tab reads from that table, and the SBX Watchlist
cross-references each listing's owner_agency against it so Alex sees a
"PREQUALIFIED" pill on bids he can actually compete for.

Required env:
  GMAIL_TOKEN_JSON       -- OAuth token (same one used by parse-gmail-quotes)
  CLAUDE_API_KEY         -- for structured extraction
  SUPABASE_SERVICE_KEY   -- to write rows
"""

from __future__ import annotations

import base64
import datetime as dt
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

SCRIPTS_DIR = Path(__file__).resolve().parent
SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "prequal_approvals_cloud"
SBX_TABLE = "sbx_listings_cloud"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# gmail.modify is the canonical scope for the shared GMAIL_TOKEN_JSON --
# subsumes readonly + send + labels. Asserting a narrower scope here would
# fail token refresh with `invalid_scope` (Google treats scope strings as
# discrete identifiers, not a hierarchy).
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
# Window kept narrow now that the historical backlog is fully backfilled in
# Supabase. Each cron tick should only sweep recent mail. The dedup check
# against prequal_approvals_cloud below means even messages inside this
# window are only classified by Claude on the FIRST run that sees them.
# Set BACKFILL_DAYS (e.g. =365) to widen the sweep — combine with
# PREQUAL_FORCE_RECLASSIFY=1 to re-classify everything in the wider window.
_BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "0") or "0")
_WINDOW_DAYS = _BACKFILL_DAYS if _BACKFILL_DAYS > 0 else 30
GMAIL_QUERY = (
    'from:kim@fusionelectric-inc.com '
    '(subject:prequal OR subject:"Pre-Qualification" OR subject:"pre-qualification" '
    'OR "approved to bid" OR "Approval" OR "expir" OR "renewed" OR "rescind") '
    f'newer_than:{_WINDOW_DAYS}d'
)


# --- Gmail auth -------------------------------------------------------------

def gmail_service():
    token_env = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not token_env:
        raise SystemExit("GMAIL_TOKEN_JSON env var is required.")
    creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- Body extraction --------------------------------------------------------

def _decode(data):
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(html):
    if not html:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def extract_body(payload):
    if not payload:
        return ""
    parts = payload.get("parts") or []
    if not parts and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    plain, html = "", ""
    for p in parts:
        mt = p.get("mimeType", "")
        if mt.startswith("multipart/"):
            sub = extract_body(p)
            if sub and not plain:
                plain = sub
        elif mt == "text/plain" and not plain:
            plain = _decode(p.get("body", {}).get("data", ""))
        elif mt == "text/html" and not html:
            html = _decode(p.get("body", {}).get("data", ""))
    return plain if plain else _strip_html(html)


# --- Agency-name fallback ---------------------------------------------------

# Words to strip from subject lines when guessing an agency name.
_SUBJECT_NOISE = re.compile(
    r"\b(?:RE|FW|FWD|Re|Fw|Fwd|Pre[- ]?Qual(?:ification)?s?|Prequal(?:ification)?s?"
    r"|Approval|Approved|Renewal|Renewed|Notification|Notice|Status|Update|Confirmation"
    r"|Application|Submit(?:ted|tal)?|Pending|Review)\b",
    re.IGNORECASE,
)
_SUBJECT_DASH = re.compile(r"\s*[-–:|]\s*")


def agency_from_subject(subject):
    """Best-effort regex extract for when Claude returns null/empty agency.
    The subject line almost always carries the agency in plain text after
    stripping prequal-related noise words and leading/trailing punctuation.
    Returns "" if nothing useful is left."""
    if not subject:
        return ""
    s = subject.strip()
    # Drop bracketed labels like "[EXTERNAL]" that some forwards add.
    s = re.sub(r"\[[^\]]+\]", " ", s)
    # Strip noise words.
    s = _SUBJECT_NOISE.sub(" ", s)
    # Split on punctuation separators; keep the longest segment, which is
    # usually the bare agency name once the prequal vocabulary is gone.
    parts = [p.strip(" -–:|") for p in _SUBJECT_DASH.split(s) if p.strip(" -–:|")]
    parts.sort(key=len, reverse=True)
    out = parts[0] if parts else s
    # Collapse whitespace, strip leftover punctuation/quotes.
    out = re.sub(r"\s+", " ", out).strip(" \"'-–:|.")
    # Reject if reduced to noise (under 4 chars or pure digits).
    if len(out) < 4 or out.isdigit():
        return ""
    return out[:200]


# --- Claude classification --------------------------------------------------

def llm_extract(subject, body, *, api_key):
    """Returns dict with structured prequal fields, or None on failure."""
    prompt = f"""You are extracting structured data from an email about a CONSTRUCTION CONTRACTOR PREQUALIFICATION at a public agency (school district, city, county, university). Fusion Electric is the contractor.

Return ONLY a JSON object (no prose, no markdown). Use null when unknown.

{{
  "is_prequal_notice": true|false,
  "agency_name": "canonical agency name e.g. 'Sequoia Union High School District'" or null,
  "agency_aliases": ["other forms the same agency might use","..."],
  "status": "approved" | "renewed" | "submitted" | "under review" | "denied" | "rescinded" | "expired" | null,
  "approval_amount": 1465800.00 (numeric dollar limit if stated) or null,
  "application_number": "977391" or null,
  "approval_date": "YYYY-MM-DD" or null,
  "expiration_date": "YYYY-MM-DD" or null,
  "notes": "Kim's commentary in her forwarded message (skip the legal boilerplate)" or null,
  "signals": ["short phrases that drove your classification"]
}}

STATUS taxonomy — pick the most specific match:
- "approved"     = agency just granted prequal (new approval)
- "renewed"      = prior approval was renewed/extended before expiration
- "submitted"    = Kim sent the application to the agency, awaiting response
- "under review" = agency acknowledged receipt and is reviewing
- "denied"       = agency declined the application
- "rescinded"    = previously approved, now revoked
- "expired"      = approval lapsed past its expiration date
Default to null only when the email is genuinely a prequal notice but you cannot tell the status from any signal.

AGENCY NAME — extract aggressively. If the body is sparse, the SUBJECT
LINE almost always names the agency (e.g. "Prequalification - Sequoia
Union HSD", "FW: City of Hayward Prequal Renewal"). Strip generic words
like "Prequal", "Pre-Qualification", "Approval", "RE:", "FW:". Never
return "Unknown" — return null and the caller will fall back to a
regex over the subject.

CRITICAL RULES for date fields:
1. approval_date: ONLY return a date if the email explicitly states the
   prequal was approved on that date (e.g. "approved on 5/4/2025",
   "approval issued: May 4, 2025"). DO NOT extract dates from quoted reply
   chains, signature lines, or unrelated context (a "On Fri, May 2..." line
   is the date someone else wrote a message, NOT the approval date). When
   in doubt, return null -- the caller fills it in from the email's
   received-at timestamp.
2. expiration_date: ONLY return a date if the email explicitly mentions an
   expiration / "valid until" / "good through" date. DO NOT compute an
   expiration from approval_date. DO NOT assume "typical 1 year" or any
   default duration. If no expiration is stated, return null. A null
   expiration means "indefinite / not stated" -- the UI must NOT mark such
   prequals as "expired".

EMAIL:
Subject: {subject}
Body (truncated to 6000 chars):
{(body or '')[:6000]}
"""
    body_bytes = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body_bytes, method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [llm-warn] HTTP {e.code}: {e.read()[:200]!r}")
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
        return json.loads(text)
    except Exception:  # noqa: BLE001
        print(f"  [llm-warn] non-JSON: {text[:200]!r}")
        return None


# --- Supabase upsert + dedup ------------------------------------------------

def load_existing_message_ids():
    """Pull the set of Gmail message IDs already classified into
    prequal_approvals_cloud. Used to skip Claude calls on messages we've
    already processed — the script is idempotent (upsert by id), but
    re-classifying is pure token waste once a row exists.

    Returns an empty set on any error so the script falls back to
    full-classify behavior (correctness preserved over efficiency).
    """
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        return set()
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=id"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return set()
            return {row.get("id") for row in json.loads(resp.read()) if row.get("id")}
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] could not load existing IDs for dedup: {e}")
        return set()


def supabase_upsert(rows):
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sent = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        req = urllib.request.Request(url, data=json.dumps(chunk).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201, 204):
                    print(f"  [warn] upsert HTTP {resp.status}")
                else:
                    sent += len(chunk)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"  [warn] upsert HTTP {e.code}: {body[:300]}")
    return sent


# --- Main -------------------------------------------------------------------

def main():
    api_key = (os.environ.get("CLAUDE_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("CLAUDE_API_KEY env var required.")

    svc = gmail_service()
    print(f"Searching Gmail: {GMAIL_QUERY}")

    page_token = None
    candidates = []
    while True:
        kwargs = {"userId": "me", "q": GMAIL_QUERY, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = svc.users().messages().list(**kwargs).execute()
        candidates.extend(resp.get("messages", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token or len(candidates) >= 500:
            break
    print(f"Found {len(candidates)} candidate messages.")

    # Pre-load already-classified Gmail IDs so we can skip Claude on
    # messages whose row already exists. Once a prequal email is in
    # Supabase its content doesn't change — re-asking Haiku is pure waste.
    # If Supabase is unreachable we fall back to an empty set (full classify).
    # PREQUAL_FORCE_RECLASSIFY=1 bypasses this dedup so a single backfill
    # run can re-classify every recent row with an improved prompt.
    force_reclassify = bool(int(os.environ.get("PREQUAL_FORCE_RECLASSIFY", "0") or "0"))
    if force_reclassify:
        existing_ids = set()
        print("PREQUAL_FORCE_RECLASSIFY=1: re-classifying every message in window.")
    else:
        existing_ids = load_existing_message_ids()
        print(f"Skipping {len(existing_ids)} message(s) already classified in {SUPABASE_TABLE}.")

    rows = []
    skipped_existing = 0
    for i, m in enumerate(candidates, 1):
        # Cheap dedup: if we've already classified this exact Gmail
        # message before, skip it without even fetching the full body.
        # Saves a Gmail API roundtrip AND a Claude call.
        if m["id"] in existing_ids:
            skipped_existing += 1
            continue
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        sender_addr = parseaddr(sender)[1].lower()
        # Defensive: skip messages NOT actually from Kim (e.g. her replies thread back to her).
        if "kim@fusionelectric-inc.com" not in sender_addr:
            continue
        body = extract_body(full.get("payload", {}))
        if len(body) < 80:
            continue  # too short to be a real prequal notice
        ext = llm_extract(subject, body, api_key=api_key)
        if not ext or not ext.get("is_prequal_notice"):
            continue

        received_ms = int(full.get("internalDate", 0))
        received_dt = dt.datetime.utcfromtimestamp(received_ms / 1000) if received_ms else None
        received_iso = received_dt.isoformat() + "Z" if received_dt else None

        # Fallback: if Claude couldn't find an explicit approval date in
        # the body (which is correct per the prompt's rules), use the
        # email's received-at date instead. This avoids the prior bug where
        # Claude was inventing dates from quoted thread chains.
        approval_date = ext.get("approval_date")
        status_lower = (ext.get("status") or "").lower()
        if not approval_date and status_lower in ("approved", "renewed") and received_dt:
            approval_date = received_dt.strftime("%Y-%m-%d")

        # Agency name resolution: Claude → regex on subject → "Unknown".
        # The regex fallback handles cases where Claude returns null
        # because the body is sparse but the subject names the agency.
        # Without this, ~13% of rows landed as agency_name="Unknown".
        agency_clean = (ext.get("agency_name") or "").strip()
        if not agency_clean:
            agency_clean = agency_from_subject(subject)
            if agency_clean:
                print(f"  [agency-fallback] '{subject[:60]}' -> '{agency_clean}'")
        if not agency_clean:
            agency_clean = "Unknown"
            print(f"  [agency-unknown] subject={subject[:80]!r} id={m['id']}")

        rows.append({
            "id":               m["id"],
            "agency_name":      agency_clean[:300],
            "agency_aliases":   ext.get("agency_aliases") or [],
            "status":           (ext.get("status") or "approved")[:30],
            "approval_amount":  ext.get("approval_amount"),
            "application_number": (ext.get("application_number") or None),
            "approval_date":    approval_date,
            "expiration_date":  ext.get("expiration_date"),  # null when not explicitly stated
            "notes":            (ext.get("notes") or "")[:1000],
            "source_subject":   subject[:500],
            "source_from":      sender_addr[:200],
            "source_received_at": received_iso,
            "raw_body":         (body or "")[:8000],
            "classifier_signals": ext.get("signals") or [],
            "generated_at":     dt.datetime.utcnow().isoformat() + "Z",
            "updated_at":       dt.datetime.utcnow().isoformat() + "Z",
        })
        if i % 10 == 0:
            print(f"  [{i}/{len(candidates)}] {len(rows)} prequal rows so far...")

    print(f"\nUpserting {len(rows)} prequal approvals to {SUPABASE_TABLE}...")
    sent = supabase_upsert(rows)
    print(f"Wrote {sent} row(s).")
    by_agency = {}
    for r in rows:
        by_agency[r["agency_name"]] = r["status"]
    print(f"Distinct agencies: {len(by_agency)}")
    for ag, st in sorted(by_agency.items())[:25]:
        print(f"  [{st:>10}] {ag}")

    # Cross-reference SBX listings: for each unique active prequal agency,
    # ask Claude which of the current SBX owner_agency strings refer to
    # the same legal entity, then write the validated set onto the prequal
    # row. Frontend matches by exact membership, so wrong matches like
    # SAMTRANS lighting up because of "San Mateo" overlap are eliminated.
    #
    # Skip the entire block when no new rows were inserted this run AND
    # we aren't being asked to force a refresh — without new prequal rows
    # there's nothing to validate that wasn't validated last run. SBX-owner
    # drift is handled by clearing validated_owner_strings manually when
    # the SBX taxonomy changes meaningfully.
    force = bool(int(os.environ.get("PREQUAL_FORCE_REVALIDATE", "0") or "0"))
    if not rows and not force:
        print("\nNo new prequal rows this run — skipping SBX cross-reference (set PREQUAL_FORCE_REVALIDATE=1 to override).")
    else:
        print(f"\nCross-referencing prequal agencies vs current SBX listings via Claude...")
        cross_reference_sbx_owners(api_key, force=force)


def cross_reference_sbx_owners(api_key, *, force=False):
    """Build prequal_approvals_cloud.validated_owner_strings by asking
    Claude which SBX owner_agency strings actually refer to each prequal
    agency. Only validates ACTIVE prequals (approved/renewed).

    Per-row dedup: skip prequals whose validated_owner_strings is already
    populated. Once a prequal is validated against the SBX corpus, the
    answer doesn't change unless either (a) the prequal's canonical
    agency_name changes — would be a new row anyway via dedup-by-id —
    or (b) SBX owners drift enough to warrant re-validation, which the
    caller signals via force=True (PREQUAL_FORCE_REVALIDATE=1)."""
    # 1) Pull every distinct SBX owner_agency for electrical-flagged listings.
    qs = "select=owner_agency&is_electrical=eq.true"
    status, body = _sb_request("GET", f"{SBX_TABLE}?{qs}")
    if status != 200:
        print(f"  [warn] could not load SBX owners: HTTP {status}")
        return
    sbx_owners = sorted({(r.get("owner_agency") or "").strip()
                         for r in json.loads(body) if r.get("owner_agency")})
    print(f"  SBX distinct electrical owners: {len(sbx_owners)}")

    # 2) Pull all active prequals — include validated_owner_strings so we
    #    can skip ones that already have a validation result.
    qs = ("status=in.(approved,renewed)&"
          "select=id,agency_name,agency_aliases,validated_owner_strings")
    status, body = _sb_request("GET", f"{SUPABASE_TABLE}?{qs}")
    if status != 200:
        print(f"  [warn] could not load active prequals: HTTP {status}")
        return
    prequals = json.loads(body)
    print(f"  Active prequals to cross-reference: {len(prequals)}")

    # 3) For each prequal, narrow candidates to plausible substring overlaps,
    #    then have Claude make the final call. Skip if already validated
    #    unless force=True.
    updates = 0
    skipped = 0
    for pq in prequals:
        canon = (pq.get("agency_name") or "").strip()
        if not canon:
            continue
        if not force and pq.get("validated_owner_strings") is not None:
            # Already validated — list can be empty (legitimately no SBX match)
            # or populated. Either way, don't re-pay for Claude.
            skipped += 1
            continue
        candidates = candidate_owners_for(canon, pq.get("agency_aliases") or [], sbx_owners)
        if not candidates:
            _patch_validated(pq["id"], [])
            continue
        validated = ai_filter_same_entity(canon, candidates, api_key=api_key)
        _patch_validated(pq["id"], validated)
        if validated:
            updates += 1
            print(f"  [✓] {canon[:40]:40}  matched {len(validated)} SBX owner(s): {[v[:40] for v in validated[:3]]}")
    print(f"  Updated validated_owner_strings on {updates} prequal row(s); skipped {skipped} already-validated.")


def candidate_owners_for(canon_agency, aliases, sbx_owners):
    """Cheap pre-filter: only feed Claude SBX owners that share a meaningful
    token (>= 6 chars) with the prequal agency. Avoids paying for Claude
    calls on obviously-unrelated pairs."""
    canon_lc = canon_agency.lower()
    needles = set()
    for source in [canon_agency] + (aliases or []):
        for tok in re.findall(r"[a-z]{6,}", (source or "").lower()):
            if tok in {"school", "district", "county", "department", "transit",
                       "community", "college", "service", "services", "authority"}:
                continue
            needles.add(tok)
    if not needles:
        return []
    out = []
    for owner in sbx_owners:
        ol = owner.lower()
        if any(n in ol for n in needles):
            out.append(owner)
    return out[:30]  # cap so the prompt stays small


def ai_filter_same_entity(canon_agency, candidate_owners, *, api_key):
    """Send (prequal canonical agency, candidate SBX owner strings) to
    Claude. Returns the subset that Claude confirms refer to the SAME
    legal entity. Defaults to [] on any error."""
    if not candidate_owners or not api_key:
        return []
    prompt = (
        "You are validating that two text representations refer to the SAME public-agency legal entity in California construction bidding.\n\n"
        f"Reference agency (Fusion Electric is pre-qualified with this entity):\n"
        f"  \"{canon_agency}\"\n\n"
        "Candidate owner_agency strings from the Sacramento Builders Exchange (SBX) listings page:\n"
        + "\n".join(f"  {i+1}. \"{o}\"" for i, o in enumerate(candidate_owners))
        + "\n\nReturn ONLY a JSON object:\n"
          "{ \"matches\": [list of EXACT candidate strings that refer to the same legal entity as the reference agency], \"reasoning\": \"one sentence\" }\n\n"
          "Be strict. 'San Mateo Foster City School District' is NOT the same entity as 'San Mateo County Transit District' even though both contain 'San Mateo'. Sub-departments / divisions within the same agency DO count (e.g. 'City of Hayward — Public Works' matches 'City of Hayward')."
    )
    body_bytes = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 600,
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
    except Exception as e:  # noqa: BLE001
        print(f"  [llm-warn] {canon_agency[:30]}: {e}")
        return []
    text = ""
    for c in data.get("content", []):
        if c.get("type") == "text":
            text += c.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
        m = obj.get("matches") or []
        # Defensive: only keep ones that were actually in our candidate list.
        cand_set = set(candidate_owners)
        return [s for s in m if isinstance(s, str) and s in cand_set]
    except Exception:  # noqa: BLE001
        return []


def _patch_validated(prequal_id, owner_list):
    """PATCH only the validated_owner_strings column on a prequal row."""
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        return
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{urllib.parse.quote(prequal_id)}"
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    body = json.dumps({
        "validated_owner_strings": owner_list,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
    }).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        print(f"  [warn] PATCH validated_owner_strings failed for {prequal_id}: HTTP {e.code} {e.read()[:200]!r}")


def _sb_request(method, path):
    """Local helper used only by cross-reference -- mirrors send-job-walk-invites
    pattern. Mainline upsert uses supabase_upsert above."""
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "content-type": "application/json"}
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


if __name__ == "__main__":
    main()
