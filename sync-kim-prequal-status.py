#!/usr/bin/env python3
"""
sync-kim-prequal-status.py — Monitors Gmail threads involving Kim + prequal keywords.
For each thread, reads the most recent message and determines status based on sender + content.
Syncs to Supabase pending_prequals_cloud table.

Statuses:
  PENDING KIM     — Gabriel/PE asked Kim, waiting for her to file
  UNDER REVIEW    — Kim submitted, waiting for agency response
  APPROVED        — Agency approved
  RENEWED         — Agency renewed
  EXPIRED         — Agency denied/expired/rescinded
"""

import json
import os
import sys
import base64
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import urllib.request
import urllib.parse


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def gmail_service():
    """Build authenticated Gmail service."""
    token_env = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not token_env:
        raise SystemExit("GMAIL_TOKEN_JSON env var is required")
    creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode(data: str) -> str:
    """Decode base64url Gmail payload."""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def extract_body(payload: Dict[str, Any]) -> str:
    """Extract text body from Gmail payload, preferring plain text."""
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


def extract_headers(payload: Dict[str, Any]) -> Dict[str, str]:
    """Extract email headers as dict."""
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def load_existing_thread_ids(sb_key: str) -> set:
    """Load thread IDs already in pending_prequals_cloud."""
    url = f"{SUPABASE_URL}/rest/v1/pending_prequals_cloud?select=thread_id"
    req = urllib.request.Request(url)
    req.add_header("apikey", sb_key)
    req.add_header("Authorization", f"Bearer {sb_key}")
    try:
        with urllib.request.urlopen(req) as r:
            rows = json.loads(r.read().decode())
            return {row["thread_id"] for row in rows}
    except Exception as e:
        print(f"Warning: could not load existing threads: {e}")
        return set()


def fetch_threads(svc, query: str, max_results: int = 100) -> List[str]:
    """Fetch thread IDs matching Gmail query."""
    try:
        results = svc.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
        return [t["id"] for t in results.get("threads", [])]
    except HttpError as e:
        print(f"Gmail error fetching threads: {e}")
        return []


def get_thread_full(svc, thread_id: str) -> Optional[Dict[str, Any]]:
    """Fetch complete thread with all messages."""
    try:
        return svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except HttpError as e:
        print(f"Error fetching thread {thread_id}: {e}")
        return None


def classify_with_claude(
    agency_name: str,
    thread_subject: str,
    first_sender: str,
    last_sender: str,
    last_message_body: str,
) -> Dict[str, Any]:
    """Use Claude Haiku to classify prequal status and extract metadata."""
    client = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY"))

    prompt = f"""Analyze this prequalification email thread and extract structured data.

Thread subject: {thread_subject}
First message from: {first_sender}
Most recent message from: {last_sender}
Most recent message body (first 2000 chars):
{last_message_body[:2000]}

Determine:
1. agency_name: The prequalification agency name (normalize: "Berkeley Unified School District" not "Berkeley USD")
2. status: One of PENDING KIM, UNDER REVIEW, APPROVED, RENEWED, EXPIRED
   - PENDING KIM if most recent message is from Gabriel/estimator asking Kim to file
   - UNDER REVIEW if Kim just sent it to an agency
   - APPROVED if agency approved prequalification
   - RENEWED if agency renewed existing prequalification
   - EXPIRED if agency denied/expired/rescinded
3. requested_by: Person who first asked for the prequalification (from first message sender if applicable)
4. submitted_date: Date Kim submitted (if stated; otherwise null)
5. notes: Any relevant notes from the thread

Return ONLY valid JSON, no markdown:
{{
  "agency_name": "...",
  "status": "...",
  "requested_by": "Gabriel" or "Jade" or null,
  "submitted_date": "2026-05-06" or null,
  "notes": "..."
}}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        return json.loads(msg.content[0].text)
    except json.JSONDecodeError as e:
        print(f"Claude response parse error: {e}")
        print(f"Raw response: {msg.content[0].text}")
        return {"status": "UNKNOWN", "notes": f"Parse error: {e}"}


def upsert_to_supabase(data: List[Dict[str, Any]], sb_key: str) -> bool:
    """Upsert rows to pending_prequals_cloud."""
    if not data:
        return True

    url = f"{SUPABASE_URL}/rest/v1/pending_prequals_cloud"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("apikey", sb_key)
    req.add_header("Authorization", f"Bearer {sb_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")

    try:
        with urllib.request.urlopen(req) as r:
            print(f"Upserted {len(data)} rows")
            return True
    except urllib.error.HTTPError as e:
        print(f"Supabase upsert error: {e.read().decode()}")
        return False


def main():
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not sb_key:
        raise SystemExit("SUPABASE_SERVICE_KEY env var is required")

    svc = gmail_service()

    # Gmail query: threads involving Kim + prequal keywords in last 90 days
    query = (
        "(from:kim@fusionelectric-inc.com OR to:kim@fusionelectric-inc.com OR cc:kim@fusionelectric-inc.com) "
        '(prequal OR "pre-qual" OR "pre-qualification") newer_than:90d'
    )

    print(f"Searching Gmail with query: {query}")
    thread_ids = fetch_threads(svc, query, max_results=100)
    print(f"Found {len(thread_ids)} threads")

    # Load already-processed threads
    existing = load_existing_thread_ids(sb_key)
    new_thread_ids = [t for t in thread_ids if t not in existing]
    print(f"Processing {len(new_thread_ids)} new threads")

    rows_to_insert = []

    for thread_id in new_thread_ids:
        thread = get_thread_full(svc, thread_id)
        if not thread or not thread.get("messages"):
            print(f"Skipping thread {thread_id} — no messages")
            continue

        messages = sorted(thread["messages"], key=lambda m: int(m.get("internalDate", 0)))

        # First and last message
        first_msg = messages[0]
        last_msg = messages[-1]

        first_headers = extract_headers(first_msg["payload"])
        last_headers = extract_headers(last_msg["payload"])
        last_body = extract_body(last_msg["payload"])

        first_sender_email = first_headers.get("from", "unknown")
        last_sender_email = last_headers.get("from", "unknown")
        thread_subject = first_headers.get("subject", "")

        # Classify with Claude
        classification = classify_with_claude(
            agency_name="",  # Claude will extract this
            thread_subject=thread_subject,
            first_sender=first_sender_email,
            last_sender=last_sender_email,
            last_message_body=last_body,
        )

        if classification.get("status") == "UNKNOWN":
            print(f"Skipping thread {thread_id} — could not classify")
            continue

        # Convert timestamp
        last_ts = int(last_msg.get("internalDate", "0"))
        last_message_at = datetime.fromtimestamp(last_ts / 1000).isoformat()

        row = {
            "thread_id": thread_id,
            "gmail_thread_id": thread_id,
            "agency_name": classification.get("agency_name", "Unknown"),
            "status": classification.get("status", "UNKNOWN"),
            "submitted_date": classification.get("submitted_date"),
            "requested_by": classification.get("requested_by"),
            "last_sender": last_sender_email,
            "last_message_at": last_message_at,
            "notes": classification.get("notes", ""),
            "updated_at": datetime.now().isoformat(),
        }
        rows_to_insert.append(row)
        print(f"  → {row['agency_name']} ({row['status']})")

    if rows_to_insert:
        success = upsert_to_supabase(rows_to_insert, sb_key)
        sys.exit(0 if success else 1)
    else:
        print("No new threads to process")
        sys.exit(0)


if __name__ == "__main__":
    main()
