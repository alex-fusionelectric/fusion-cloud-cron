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
CURRENT_ROOT  = "ESTIMATING/CURRENT BIDS"
SENT_ROOT     = "ESTIMATING/SENT BIDS"
ROOT_LABEL    = CURRENT_ROOT  # back-compat alias
ACTIVE_STATUS = {"BIDDING", "BID OR BAIL", "SENT", "FOLLOW UP", "FOLLOW UPS", "PENDING"}
# Which folder each status belongs in. Used to decide where a brand-new label
# goes, so we NEVER recreate a CURRENT label for a bid that has already
# (correctly) moved to SENT -- that recreation was the duplicate-label bug.
CURRENT_STATUSES = {"BIDDING", "BID OR BAIL", "PENDING"}
SENT_STATUSES    = {"SENT", "FOLLOW UP", "FOLLOW UPS"}
# Match an EST# label in EITHER folder. Counting a SENT label as "already
# exists" is what stops the duplicate-label churn with the relabeler: once a
# bid's label has been renamed into SENT BIDS, this job sees it and leaves it
# alone instead of forging a fresh CURRENT BIDS copy.
EST_LABEL_RX  = re.compile(r"^ESTIMATING/(?:CURRENT|SENT) BIDS/(\d{2}-\d{3,4})\b", re.IGNORECASE)


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
    """Return {est_number: label_name} for every EST# label, whether it lives
    under ESTIMATING/CURRENT BIDS/* or ESTIMATING/SENT BIDS/*. Scanning both
    folders means a bid that has moved to SENT still counts as 'has a label',
    so we don't recreate a duplicate in CURRENT."""
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
    """Fetch active BAY-division bids from bids_cloud.

    Gmail labels under ESTIMATING/CURRENT BIDS/ are Alex's BAY inbox only.
    SAC bids belong to Austin and must NOT get labels here.
    """
    status_in = ",".join(urllib.parse.quote(s, safe="") for s in ACTIVE_STATUS)
    st, body = _sb("GET", f"bids_cloud?select=est_number,project_name,status,outcome,division"
                          f"&status=in.({status_in})&division=eq.BAY&limit=200")
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
            # No label in EITHER folder -- create one in the folder that
            # matches this bid's status. A sent bid gets a SENT BIDS label,
            # NOT a CURRENT one (recreating CURRENT was the duplicate bug).
            status = (bid.get("status") or "").strip().upper()
            root = SENT_ROOT if status in SENT_STATUSES else CURRENT_ROOT
            label_name = f"{root}/{est} {name}"
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

    # Scan Gmail labels for each bid to find GCs who have emailed into the label.
    # Any external sender (not @fusionelectric-inc.com) in the bid's label thread
    # is a potential GC — write to sbx_plan_holders_cloud so the card shows them.
    gc_rows = []
    FUSION_DOMAIN = "fusionelectric-inc.com"
    for est, label_name in existing.items():
        if not label_name:
            continue
        try:
            # Get up to 20 thread senders from this label
            threads_resp = svc.users().threads().list(
                userId="me",
                labelIds=[next((L["id"] for L in svc.users().labels().list(userId="me").execute().get("labels",[])
                                if L.get("name") == label_name), "")],
                maxResults=20
            ).execute()
            for t in threads_resp.get("threads", []) or []:
                thread = svc.users().threads().get(userId="me", id=t["id"], format="minimal").execute()
                for msg in (thread.get("messages") or [])[:5]:
                    headers = {h["name"].lower(): h["value"]
                               for h in (msg.get("payload", {}).get("headers") or [])}
                    frm = headers.get("from", "")
                    addr = re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", frm)
                    if not addr:
                        continue
                    email = addr.group(0).lower()
                    if FUSION_DOMAIN in email:
                        continue
                    org = email.split("@")[1] if "@" in email else email
                    name_part = frm.split("<")[0].strip().strip('"') or org
                    nm_norm = re.sub(r"\s+", " ", name_part.upper().replace("&", " AND ")).strip()
                    if not nm_norm or len(nm_norm) < 3:
                        continue
                    gc_rows.append({
                        "id": f"{est}::GMAIL::{nm_norm[:40]}",
                        "opsplannum": est,
                        "gc_name": name_part[:120],
                        "gc_name_normalized": nm_norm[:120],
                        "contact_email": email,
                        "status": "active",
                        "last_seen_at": now_iso,
                        "updated_at": now_iso,
                    })
        except Exception as _ge:
            pass  # missing label or API error — skip silently

    # Dedupe by id before upsert
    seen_ids: set[str] = set()
    deduped = []
    for g in gc_rows:
        if g["id"] not in seen_ids:
            seen_ids.add(g["id"])
            deduped.append(g)

    if deduped:
        for i in range(0, len(deduped), 50):
            chunk = deduped[i:i + 50]
            st_g, _ = _sb("POST",
                          f"sbx_plan_holders_cloud?on_conflict=id",
                          body=chunk)
            if st_g not in (200, 201, 204):
                print(f"  [warn] gc upsert HTTP {st_g}")
        print(f"  Wrote {len(deduped)} GC contacts from Gmail labels")

    print(f"\nDone: {created} label(s) created, "
          f"{updated_realtime}/{len(bids)} realtime rows synced, "
          f"{len(deduped)} GC contacts from Gmail labels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
