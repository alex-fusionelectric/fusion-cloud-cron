"""dismiss-set-up-invitations.py -- mark Bid Radar invitations as handled
when their Gmail thread already carries an EST# label.

Why this exists
===============
Bid Radar surfaces bid invitation emails so Alex can decide whether to set
up the bid in BAY PowerBid. Once a bid IS set up, its source-invitation
thread doesn't auto-disappear from the radar -- it lingers, cluttering the
queue and making "what needs my attention" harder to read.

Alex's insight (2026-05-04): the moment a bid is set up, the thread gets
labeled in Gmail under `ESTIMATING/CURRENT BIDS/YY-NNN PROJECT NAME`. That
EST# label IS the canonical "this is handled" signal -- way stronger than
any fuzzy name match between bid_invitations.subject and BID LIST short
names. So this cron:

    1. Pulls every bid_invitations row where is_invitation = true
    2. For each, fetches the thread from Gmail and reads its labels
    3. If any label matches `ESTIMATING/CURRENT BIDS/YY-NNN *`, flips
       is_invitation=false (= dismissed). Frontend already filters out
       is_invitation=false rows.
    4. Idempotent: re-running picks up newly-set-up bids without
       touching ones it already dismissed.

Excluded labels (NOT used as the dismiss signal):
    - "ESTIMATING/CURRENT BIDS/00-POTENTIAL BIDS" -- staging label,
       human-flagged "look at this", NOT a setup confirmation.

Required env (matches the other crons in this repo):
    GMAIL_TOKEN_JSON       -- OAuth token (gmail.modify scope, as set
                              after the 2026-05-04 token re-mint)
    SUPABASE_SERVICE_KEY   -- needed to UPDATE bid_invitations (anon
                              key is RLS-blocked from writes)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "bid_invitations"

# gmail.modify is the canonical scope for the shared GMAIL_TOKEN_JSON.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# An EST# label looks like:  ESTIMATING/CURRENT BIDS/26-232 SHC PACU ...
# We DELIBERATELY exclude the staging label (00-POTENTIAL BIDS) and any
# label that is just the parent ("ESTIMATING/CURRENT BIDS"). Only the
# leaf-with-EST# pattern counts as "the bid is set up."
EST_LABEL_RX = re.compile(
    r"^ESTIMATING/CURRENT\s+BIDS/(\d{2}-\d{3,4})\b",
    re.IGNORECASE,
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


# --- Supabase REST helpers --------------------------------------------------

def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required (anon can't UPDATE bid_invitations).")
    return k


def sb_get(path: str) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": _service_key(),
        "Authorization": f"Bearer {_service_key()}",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sb_patch_bulk_dismiss(id_to_est: dict) -> int:
    """Set is_invitation=false on the given invitation ids.
    id_to_est: {invitation_id: est_number} — est_number logged but not
    written to DB (bid_invitations has no est_number column yet).
    Done in chunks of 50."""
    if not id_to_est:
        return 0
    ids = list(id_to_est.keys())
    sent = 0
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        in_clause = ",".join(urllib.parse.quote(x, safe="") for x in chunk)
        url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=in.({in_clause})"
        body = json.dumps({"is_invitation": False}).encode("utf-8")
        req = urllib.request.Request(
            url, method="PATCH", data=body,
            headers={
                "apikey": _service_key(),
                "Authorization": f"Bearer {_service_key()}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status in (200, 204):
                    sent += len(chunk)
                else:
                    print(f"  [warn] PATCH HTTP {r.status} on chunk {i//50}")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  [warn] PATCH HTTP {e.code}: {err[:300]}")
    return sent


# --- Main loop --------------------------------------------------------------

def main() -> int:
    svc = gmail_service()

    # Fetch label name -> id map. Cheaper than calling per-thread.
    print("Fetching Gmail label index...")
    labels_resp = svc.users().labels().list(userId="me").execute()
    label_id_to_name = {L["id"]: L.get("name", "") for L in labels_resp.get("labels", [])}
    est_label_ids = {
        lid for lid, name in label_id_to_name.items()
        if EST_LABEL_RX.match(name or "")
    }
    print(f"  {len(est_label_ids)} EST# labels found "
          f"({len(label_id_to_name)} total user labels).")

    # Pull pending invitations.
    print("Fetching pending bid_invitations...")
    rows = sb_get(f"{SUPABASE_TABLE}?select=id,subject,project_name,thread_id,message_id"
                  f"&is_invitation=eq.true&limit=2000")
    print(f"  {len(rows)} pending invitations to check.")

    to_dismiss: list[tuple[str, str, str]] = []  # (id, thread_id, est_label_name)
    skipped_no_thread = 0
    skipped_unhandled = 0

    for r in rows:
        thread_id = r.get("thread_id") or r.get("message_id")  # message_id is a fallback
        if not thread_id:
            skipped_no_thread += 1
            continue
        try:
            thread = svc.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
        except Exception as e:  # noqa: BLE001
            # 404 is expected for threads that were deleted; just skip.
            print(f"  [warn] thread {thread_id} fetch failed: {e}")
            continue
        # Union of label IDs across all messages in the thread.
        thread_label_ids = set()
        for m in thread.get("messages", []) or []:
            for lid in m.get("labelIds", []) or []:
                thread_label_ids.add(lid)
        # Any EST# label present?
        matched = thread_label_ids & est_label_ids
        if matched:
            label_name = label_id_to_name.get(next(iter(matched)), "?")
            # Extract EST# from label name e.g. "ESTIMATING/CURRENT BIDS/26-221 SHC..."
            est_match = re.match(r".*?(\d{2}-\d{3,4})\b", label_name)
            est_num = est_match.group(1) if est_match else ""
            to_dismiss.append((r["id"], thread_id, label_name, est_num))
        else:
            skipped_unhandled += 1

    print()
    print(f"To dismiss: {len(to_dismiss)} invitation(s) whose thread carries an EST# label.")
    print(f"Still pending: {skipped_unhandled} (no EST# label = legitimately needs attention).")
    print(f"Skipped (no thread_id): {skipped_no_thread}")
    if to_dismiss:
        print()
        print("Sample of what's being dismissed:")
        for inv_id, tid, ln, est in to_dismiss[:15]:
            print(f"  - {inv_id[:20]:<22} thread={tid[:20]:<22} est={est:<8} via label='{ln}'")
        if len(to_dismiss) > 15:
            print(f"  ... and {len(to_dismiss) - 15} more")

    if "--dry-run" in sys.argv:
        print()
        print("[--dry-run] No DB writes performed.")
        return 0

    id_to_est = {t[0]: t[3] for t in to_dismiss}
    written = sb_patch_bulk_dismiss(id_to_est)
    print(f"\nDismissed {written}/{len(to_dismiss)} invitation(s) in Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
