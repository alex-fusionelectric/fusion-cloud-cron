"""ensure-bid-labels.py — Guarantee every active bid in bids_cloud has a
Gmail label under ESTIMATING/CURRENT BIDS/YY-NNN PROJECT NAME.

Per Alex 2026-05-04: "Make a rule that any time a bid is active in the
bid list that a label is always forced so it never can fall off."

Runs every 30 min. Safe to run repeatedly — idempotent.

Required env: GMAIL_TOKEN_JSON, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
except ImportError as e:
    print(f"[error] google api libs missing: {e}", file=sys.stderr)
    sys.exit(2)

SUPABASE_URL  = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SCOPES        = ["https://www.googleapis.com/auth/gmail.modify"]
ROOT_LABEL    = "ESTIMATING/CURRENT BIDS"
ACTIVE_STATUS = {"BIDDING", "BID OR BAIL", "SENT", "FOLLOW UP", "FOLLOW UPS", "PENDING"}
EST_LABEL_RX  = re.compile(r"^ESTIMATING/CURRENT BIDS/(\d{2}-\d{3,4})\b", re.IGNORECASE)


def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb(method: str, path: str, body=None) -> tuple[int, bytes]:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey":        _service_key(),
        "Authorization": f"Bearer {_service_key()}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_existing_est_labels(svc) -> dict[str, str]:
    """Return {est_number: label_name} for all existing ESTIMATING/CURRENT BIDS/* labels."""
    result = svc.users().labels().list(userId="me").execute()
    out = {}
    for L in result.get("labels", []) or []:
        m = EST_LABEL_RX.match(L.get("name") or "")
        if m:
            out[m.group(1).upper()] = L["name"]
    return out


def create_label(svc, name: str) -> str:
    """Create Gmail label and return its id."""
    result = svc.users().labels().create(userId="me", body={
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }).execute()
    return result.get("id", "")


def get_active_bids() -> list[dict]:
    """Fetch active bids from bids_cloud."""
    status_in = ",".join(urllib.parse.quote(s, safe="") for s in ACTIVE_STATUS)
    st, body = _sb("GET", f"bids_cloud?select=est_number,project_name,status,outcome"
                          f"&status=in.({status_in})&limit=200")
    if st != 200:
        print(f"[warn] bids_cloud fetch failed: HTTP {st}")
        return []
    rows = json.loads(body)
    return [r for r in rows
            if (r.get("outcome") or "").lower() not in ("awarded", "not awarded")]


def main() -> int:
    print(f"=== ensure-bid-labels {datetime.now().isoformat()} ===")
    svc = gmail_service()

    existing = get_existing_est_labels(svc)
    print(f"  {len(existing)} EST# labels already in Gmail")

    bids = get_active_bids()
    print(f"  {len(bids)} active bids in BID LIST")

    created = 0
    updated_realtime = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for bid in bids:
        est = (bid.get("est_number") or "").strip().upper()
        name = (bid.get("project_name") or "").strip()
        if not est or not name:
            continue

        if est in existing:
            label_name = existing[est]
        else:
            # Label missing — create it
            label_name = f"{ROOT_LABEL}/{est} {name}"
            print(f"  + creating label: {label_name}")
            try:
                create_label(svc, label_name)
                existing[est] = label_name
                created += 1
            except Exception as e:
                print(f"  [warn] create failed for {est}: {e}")
                continue

        # Write label back to bids_realtime_cloud so the card always shows it.
        # Uses upsert-by-est_number so it's additive (doesn't clear other fields).
        st2, _ = _sb("POST",
                     "bids_realtime_cloud?on_conflict=est_number",
                     body=[{
                         "est_number":  est.upper(),
                         "project_name": name,
                         "status":       bid.get("status") or "BIDDING",
                         "gmail_label":  label_name,
                         "source":       "ensure_labels_cron",
                         "updated_at":   now_iso,
                     }])
        if st2 in (200, 201, 204):
            updated_realtime += 1
        else:
            print(f"  [warn] bids_realtime_cloud upsert {est} HTTP {st2}")

    print(f"\nDone: {created} label(s) created, "
          f"{updated_realtime}/{len(bids)} realtime rows synced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
