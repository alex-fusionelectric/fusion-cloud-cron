#!/usr/bin/env python3
"""
keep-bid-labels-synced.py -- Keep each bid's Gmail label in the right folder
based on its BID LIST status, by RENAMING the label in place.

Rule (per Alex 2026-05-27): each EST# bid has exactly ONE label. The label
lives under ESTIMATING/CURRENT BIDS while the bid is being bid, and MOVES to
ESTIMATING/SENT BIDS when status flips to SENT/AWARDED. The move is a label
rename via Gmail API `labels.patch` -- preserves the labelId, so every thread
already carrying the label automatically follows the rename. No create+delete.

Status -> folder map:
  BIDDING / BID OR BAIL          -> ESTIMATING/CURRENT BIDS
  SENT / AWARDED / FOLLOW UP*    -> ESTIMATING/SENT BIDS
  ARCHIVED                       -> leave alone (label stays where it last was)
  Other                          -> leave alone (we don't know which side)

Also: audits any *new* ESTIMATING/SENT BIDS/* label that appears while its
bid is still in BIDDING/BID OR BAIL. That's the spurious-label class we
can't yet trace -- writes one row per occurrence to bid_label_audit_cloud
so we can find the source.

Runs hourly via GitHub Actions.

Required env: GMAIL_TOKEN_JSON, SUPABASE_SERVICE_KEY (or SUPABASE_ANON_KEY)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SUPABASE_URL         = "https://dltuvsdwrujjsmiotaxy.supabase.co"
CURRENT_BIDS_FOLDER  = "ESTIMATING/CURRENT BIDS"
SENT_BIDS_FOLDER     = "ESTIMATING/SENT BIDS"
SCOPES               = ["https://www.googleapis.com/auth/gmail.modify"]

# Status -> intended folder. Any status not listed is "don't touch."
CURRENT_STATUSES = {"BIDDING", "BID OR BAIL"}
SENT_STATUSES    = {"SENT", "AWARDED", "FOLLOW UP", "FOLLOW UPS"}

# gmail_kv_cloud key for the audit snapshot
KV_TABLE             = "gmail_kv_cloud"
SNAPSHOT_KEY         = "bid_label_audit:sent_bids_snapshot"
AUDIT_TABLE          = "bid_label_audit_cloud"


# --- Supabase helpers -------------------------------------------------------

def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY")
         or os.environ.get("SUPABASE_ANON_KEY")
         or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY (or SUPABASE_ANON_KEY) env var required")
    return k


def _sb(method: str, path: str, body=None, prefer: str | None = None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey":        _service_key(),
        "Authorization": f"Bearer {_service_key()}",
        "Content-Type":  "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- Gmail helpers ----------------------------------------------------------

def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var is required")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_all_labels(svc) -> list[dict]:
    return svc.users().labels().list(userId="me").execute().get("labels", []) or []


def get_bid_statuses() -> dict[str, str]:
    """{EST#: status} for every BAY-division bid in bids_cloud. We only
    manage BAY -- SAC bids belong to Austin and live in his Gmail."""
    st, body = _sb(
        "GET",
        "bids_cloud?select=est_number,status,division&division=eq.BAY&limit=2000",
    )
    if st != 200:
        print(f"[warn] bids_cloud fetch failed: HTTP {st}")
        return {}
    out = {}
    for row in json.loads(body):
        est = (row.get("est_number") or "").strip().upper()
        status = (row.get("status") or "").strip().upper()
        if est:
            out[est] = status
    return out


# --- Label movement (rename via patch) --------------------------------------

def _parse_label(name: str) -> tuple[str, str, str] | None:
    """Split 'ESTIMATING/{CURRENT|SENT} BIDS/{EST#} {NAME}' into
    (folder, est_number, leaf_name). Returns None if not a managed label."""
    for folder in (CURRENT_BIDS_FOLDER, SENT_BIDS_FOLDER):
        prefix = f"{folder}/"
        if name.startswith(prefix):
            leaf = name[len(prefix):]
            # Leaf format: "{EST#} {NAME}", e.g. "26-282 SHC K448 PHARMACY"
            parts = leaf.split(" ", 1)
            if len(parts) >= 1 and parts[0]:
                return (folder, parts[0].upper(), leaf)
    return None


def rename_label(svc, label_id: str, new_name: str) -> tuple[bool, str]:
    """Rename a Gmail label via labels.patch. Preserves label ID -- every
    thread already tagged keeps the tag. Returns (ok, message)."""
    try:
        svc.users().labels().patch(
            userId="me", id=label_id,
            body={"name": new_name},
        ).execute()
        return True, "renamed"
    except HttpError as e:
        # 409: a label with that name already exists (the duplicate case)
        return False, f"HTTP {e.resp.status}: {str(e)[:120]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def sync_labels(svc, bid_statuses: dict[str, str], all_labels: list[dict]) -> int:
    """For each managed label, ensure it sits in the correct folder per
    the bid's BID LIST status. Renames in place when wrong; logs [dupe]
    when both folders already have a label for the same EST#."""
    # Build {est_number: {folder: label}} so we can spot duplicates
    by_est: dict[str, dict[str, dict]] = {}
    for L in all_labels:
        parsed = _parse_label(L.get("name") or "")
        if not parsed:
            continue
        folder, est, leaf = parsed
        by_est.setdefault(est, {})[folder] = {
            "id":   L["id"],
            "name": L["name"],
            "leaf": leaf,
        }

    renames = 0
    dupes = 0
    skipped_unknown_status = 0

    for est, entries in sorted(by_est.items()):
        status = bid_statuses.get(est)

        # Both folders have a label for this EST# -> duplicate. Leave alone
        # per Alex's "fix going forward" preference; just surface in logs.
        if len(entries) == 2:
            dupes += 1
            cur = entries[CURRENT_BIDS_FOLDER]
            sent = entries[SENT_BIDS_FOLDER]
            print(f"  [dupe] {est}: status={status} -- both CURRENT and SENT labels exist "
                  f"(current={cur['id']} '{cur['leaf']}', sent={sent['id']} '{sent['leaf']}')")
            continue

        # Exactly one label exists. Decide if it's in the right folder.
        if not status:
            # No BID LIST row found -- could be SAC, archived, or just not synced yet
            skipped_unknown_status += 1
            continue

        if status in CURRENT_STATUSES:
            wrong_entry = entries.get(SENT_BIDS_FOLDER)
            if wrong_entry:
                new_name = f"{CURRENT_BIDS_FOLDER}/{wrong_entry['leaf']}"
                ok, msg = rename_label(svc, wrong_entry["id"], new_name)
                if ok:
                    print(f"  [move] {est}: SENT -> CURRENT ({wrong_entry['leaf']}) "
                          f"label={wrong_entry['id']}")
                    renames += 1
                else:
                    print(f"  [warn] {est}: rename SENT->CURRENT failed: {msg}")
        elif status in SENT_STATUSES:
            wrong_entry = entries.get(CURRENT_BIDS_FOLDER)
            if wrong_entry:
                new_name = f"{SENT_BIDS_FOLDER}/{wrong_entry['leaf']}"
                ok, msg = rename_label(svc, wrong_entry["id"], new_name)
                if ok:
                    print(f"  [move] {est}: CURRENT -> SENT ({wrong_entry['leaf']}) "
                          f"label={wrong_entry['id']}")
                    renames += 1
                else:
                    print(f"  [warn] {est}: rename CURRENT->SENT failed: {msg}")
        # Any other status (ARCHIVED, blank, etc.) -- leave alone.

    print(f"  summary: {renames} renamed, {dupes} duplicates flagged, "
          f"{skipped_unknown_status} skipped (no BID LIST status)")
    return renames


# --- Audit: detect freshly-created SENT BIDS labels -------------------------

def _load_snapshot() -> set[str]:
    st, body = _sb("GET", f"{KV_TABLE}?key=eq.{SNAPSHOT_KEY}&select=value&limit=1")
    if st != 200:
        return set()
    try:
        rows = json.loads(body)
        if not rows:
            return set()
        v = rows[0].get("value") or {}
        return set(v.get("label_ids") or [])
    except Exception:
        return set()


def _save_snapshot(label_ids: set[str]) -> None:
    body = [{
        "key":   SNAPSHOT_KEY,
        "value": {"label_ids": sorted(label_ids), "updated_at": datetime.now(timezone.utc).isoformat()},
    }]
    st, _ = _sb("POST", KV_TABLE, body=body,
                prefer="resolution=merge-duplicates,return=minimal")
    if st not in (200, 201, 204):
        print(f"  [warn] snapshot save failed (HTTP {st})")


def audit_new_sent_bids(all_labels: list[dict], bid_statuses: dict[str, str]) -> int:
    """Compare the current set of SENT BIDS labels against the prior snapshot.
    For each label that newly appeared while its bid is still BIDDING/BID OR
    BAIL, write an audit row to bid_label_audit_cloud."""
    current_sent: dict[str, dict] = {}  # {label_id: {est, name}}
    for L in all_labels:
        parsed = _parse_label(L.get("name") or "")
        if parsed and parsed[0] == SENT_BIDS_FOLDER:
            current_sent[L["id"]] = {"est": parsed[1], "name": L["name"]}

    prior = _load_snapshot()
    newly_appeared = set(current_sent.keys()) - prior

    flagged = 0
    if not prior:
        # First run -- just establish the baseline, don't flag everything
        # as suspicious.
        print(f"  [audit] no prior snapshot -- recording baseline of "
              f"{len(current_sent)} SENT BIDS labels (no flags this run)")
    else:
        for label_id in newly_appeared:
            entry = current_sent[label_id]
            est    = entry["est"]
            status = bid_statuses.get(est)
            if status and status in CURRENT_STATUSES:
                print(f"  [suspicious] {est}: NEW SENT BIDS label appeared while "
                      f"status={status} -- labelId={label_id} name='{entry['name']}'")
                # Upsert audit row
                row = {
                    "est_number":             est,
                    "label_id":               label_id,
                    "label_name":             entry["name"],
                    "first_seen_at":          datetime.now(timezone.utc).isoformat(),
                    "bid_status_at_creation": status,
                }
                st, body = _sb("POST", AUDIT_TABLE, body=[row],
                               prefer="resolution=merge-duplicates,return=minimal")
                if st in (200, 201, 204):
                    flagged += 1
                else:
                    # If the table doesn't exist yet (migration not run), warn
                    # loud once and keep going -- don't crash the cron.
                    print(f"  [warn] audit insert failed (HTTP {st}): "
                          f"{body[:200] if body else ''}")

    # Save the new snapshot regardless
    _save_snapshot(set(current_sent.keys()))
    if flagged:
        print(f"  [audit] {flagged} new suspicious SENT BIDS label(s) recorded")
    return flagged


# --- Main -------------------------------------------------------------------

def main() -> int:
    print(f"=== keep-bid-labels-synced {datetime.now(timezone.utc).isoformat()} ===")
    svc          = gmail_service()
    all_labels   = get_all_labels(svc)
    bid_statuses = get_bid_statuses()

    if not bid_statuses:
        print("[warn] no BAY bids found in Supabase -- skipping sync")
        return 1

    print(f"  {len(all_labels)} total Gmail labels, "
          f"{len(bid_statuses)} BAY bids in BID LIST")

    sync_labels(svc, bid_statuses, all_labels)
    audit_new_sent_bids(all_labels, bid_statuses)

    print("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
