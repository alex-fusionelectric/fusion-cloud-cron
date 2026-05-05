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
      'subject:"subcontractor bid"'
    ") "
    "-from:fusionelectric-inc.com "
    "-from:fusionelectricinc.onmicrosoft.com"
)

# Second query: scan the 00-POTENTIAL BIDS label with NO sender filter.
# Jake and the team forward bid invitations here (BuildingConnected, etc.).
# Because the From: shows a Fusion address (forwarder), the main GMAIL_QUERY
# excludes them. This separate label scan catches everything in that folder.
POTENTIAL_BIDS_LABEL_QUERY = "label:estimating-current-bids-00-potential-bids"


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


def upsert_invitations(rows):
    if not rows:
        return 0
    status, resp = _sb_request(
        "POST", INVITATIONS_TABLE, body=rows,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if status not in (200, 201, 204):
        print(f"  [warn] upsert HTTP {status}: {resp[:300]!r}")
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


# --- Claude classification --------------------------------------------------

def llm_extract(subject, body, sender, *, api_key):
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
  "scope_hints": ["distribution","fire alarm","low voltage","lighting","security","audio visual","nurse call","trenching"] subset,
  "estimating_platform": "BuildingConnected|SmartBid|PipelineSuite|PlanHub|BidNet|PlanRoom|PlanetBids|Procore|iSqFt|direct-email|other" or null,
  "permalink": "URL to the bid portal/listing" or null
}}

EMAIL:
From: {sender}
Subject: {subject}
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
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


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
    print(f"  Pass 1: {len(candidates)} messages")

    # Pass 2: 00-POTENTIAL BIDS label (no sender filter — Jake/team forward bids
    # here from BuildingConnected etc.; the From: shows Fusion so pass 1 misses them)
    label_query = f"{POTENTIAL_BIDS_LABEL_QUERY} newer_than:{days}d"
    label_candidates = _fetch_candidates(label_query, 200)
    seen_ids = {m["id"] for m in candidates}
    new_from_label = [m for m in label_candidates if m["id"] not in seen_ids]
    candidates.extend(new_from_label)
    print(f"  Pass 2 (00-POTENTIAL BIDS label): {len(label_candidates)} found, {len(new_from_label)} new")
    print(f"Total candidates after both passes: {len(candidates)}")

    cache_keys = [f"bidinv:{m['id']}" for m in candidates]
    cache = kv_get_many(cache_keys)
    print(f"Cache hits: {len(cache)} of {len(candidates)}")

    today = dt.date.today()
    rows = []
    classified = 0
    skipped = 0
    for i, m in enumerate(candidates, 1):
        ck = f"bidinv:{m['id']}"
        ext = cache.get(ck)
        if not ext or not isinstance(ext, dict) or "is_invitation" not in ext:
            full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            body = extract_body(full.get("payload", {}))
            ext = llm_extract(subject, body, sender, api_key=api_key)
            classified += 1
            if ext is None:
                skipped += 1
                continue
            kv_upsert(ck, ext)
            time.sleep(0.2)
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
        if _loc and _OUT_OF_STATE.search(_loc) and "CA" not in _loc and "CALIFORNIA" not in _loc:
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
            meta = svc.users().messages().get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From","Subject","Date"]).execute()
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
        stable_id = hashlib.sha1(m["id"].encode("utf-8")).hexdigest()[:24]
        rows.append({
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
            "received_at":     received_iso,
            "generated_at":    dt.datetime.utcnow().isoformat() + "Z",
            "updated_at":      dt.datetime.utcnow().isoformat() + "Z",
        })

    print(f"\nClassified {classified} new (cache misses), {skipped} skipped (LLM failures).")
    print(f"Future-dated invitations to upsert: {len(rows)}")
    n = upsert_invitations(rows)
    print(f"Wrote {n} row(s) to {INVITATIONS_TABLE}.")


if __name__ == "__main__":
    main()
