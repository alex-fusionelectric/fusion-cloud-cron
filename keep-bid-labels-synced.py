#!/usr/bin/env python3
"""
keep-bid-labels-synced.py — Ensures Gmail EST# labels stay synchronized with BID LIST status.

Rule: Each EST# label should exist in only ONE folder (CURRENT or SENT) based on BID LIST status.
- BIDDING or BID OR BAIL -> label should be in ESTIMATING/CURRENT BIDS
- SENT or AWARDED -> label should be in ESTIMATING/SENT BIDS
- ARCHIVED -> label should be removed entirely (old bids)

Runs hourly via GitHub Actions.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
CURRENT_BIDS_FOLDER = "ESTIMATING/CURRENT BIDS"
SENT_BIDS_FOLDER = "ESTIMATING/SENT BIDS"


def gmail_service():
    """Build authenticated Gmail service."""
    token_env = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not token_env:
        raise SystemExit("GMAIL_TOKEN_JSON env var is required")
    creds = Credentials.from_authorized_user_info(json.loads(token_env), ["https://www.googleapis.com/auth/gmail.modify"])
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_all_labels(service):
    """Get all Gmail labels."""
    results = service.users().labels().list(userId="me").execute()
    return results.get("labels", [])


def get_bid_statuses(anon_key):
    """Fetch all EST# statuses from Supabase."""
    url = f'{SUPABASE_URL}/rest/v1/bids_cloud?select=est_number,status&limit=2000'
    req = urllib.request.Request(url)
    req.add_header("apikey", anon_key)

    try:
        with urllib.request.urlopen(req) as r:
            rows = json.loads(r.read().decode())
            return {row["est_number"]: row["status"] for row in rows if row.get("est_number")}
    except urllib.error.HTTPError as e:
        print(f"Error fetching bid statuses: {e.read().decode()}")
        return {}


def sync_labels(service, bid_statuses, all_labels):
    """Sync EST# labels with BID LIST statuses."""

    # Build label maps
    current_labels = {}
    sent_labels = {}

    for label in all_labels:
        name = label.get("name", "")
        if name.startswith(f"{CURRENT_BIDS_FOLDER}/"):
            est = name.replace(f"{CURRENT_BIDS_FOLDER}/", "").split(" ")[0]
            current_labels[est] = label["id"]
        elif name.startswith(f"{SENT_BIDS_FOLDER}/"):
            est = name.replace(f"{SENT_BIDS_FOLDER}/", "").split(" ")[0]
            sent_labels[est] = label["id"]

    fixes = 0

    # Check CURRENT BIDS labels
    for est, label_id in current_labels.items():
        status = bid_statuses.get(est)
        if status and status not in ["BIDDING", "BID OR BAIL"]:
            print(f"  {est}: status={status}, removing from CURRENT BIDS")
            try:
                results = service.users().threads().list(userId="me", q=f"label:{label_id}", maxResults=100).execute()
                for thread in results.get("threads", []):
                    service.users().threads().modify(userId="me", id=thread["id"], body={"removeLabelIds": [label_id]}).execute()
                fixes += 1
            except HttpError as e:
                print(f"    Error: {e}")

    # Check SENT BIDS labels
    for est, label_id in sent_labels.items():
        status = bid_statuses.get(est)
        if status and status in ["BIDDING", "BID OR BAIL"]:
            print(f"  {est}: status={status}, removing from SENT BIDS (should be current)")
            try:
                results = service.users().threads().list(userId="me", q=f"label:{label_id}", maxResults=100).execute()
                for thread in results.get("threads", []):
                    service.users().threads().modify(userId="me", id=thread["id"], body={"removeLabelIds": [label_id]}).execute()
                fixes += 1
            except HttpError as e:
                print(f"    Error: {e}")

    return fixes


def main():
    anon_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRsdHV2c2R3cnVqanNtaW90YXh5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzcwNDU4NDIsImV4cCI6MjA5MjYyMTg0Mn0._lMgcZgERcgVULQ87BQFrNpZBssJeNtqN5LhhGsqE8Y"

    print("Syncing Gmail EST# labels with BID LIST status...")

    svc = gmail_service()
    all_labels = get_all_labels(svc)
    bid_statuses = get_bid_statuses(anon_key)

    if not bid_statuses:
        print("Warning: no bids found in Supabase")
        sys.exit(1)

    fixes = sync_labels(svc, bid_statuses, all_labels)

    print(f"Done - fixed {fixes} label inconsistencies")
    sys.exit(0)


if __name__ == "__main__":
    main()
