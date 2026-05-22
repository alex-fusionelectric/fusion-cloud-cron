"""cleanup-auto-filed-label.py -- Audit + optionally remove the AUTO-FILED
Gmail label the auto-file-vendor-quotes cron added to every thread it
processed. Three modes via env:

  MODE=audit    list threads carrying the label, no changes (default)
  MODE=unlabel  remove the AUTO-FILED label from every tagged thread
                (keeps the label definition itself in case we re-enable
                the cron later)
  MODE=nuke     remove the label from every thread AND delete the label
                definition itself

DRY_RUN=1 prints the plan without making changes (applies to unlabel +
nuke modes).

Required env: GMAIL_TOKEN_JSON (Gmail OAuth refresh token JSON)
"""
from __future__ import annotations

import json
import os
import sys

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

LABEL_NAME    = "AUTO-FILED"
GMAIL_SCOPES  = ["https://www.googleapis.com/auth/gmail.modify"]
MODE          = (os.environ.get("MODE") or "audit").strip().lower()
DRY_RUN       = (os.environ.get("DRY_RUN") or "").strip() == "1"


def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required")
    creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def find_label(svc, name: str):
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl.get("name") == name:
            return lbl
    return None


def list_tagged_threads(svc, label_id: str) -> list[dict]:
    """Return [{id, snippet, subject?}] for every thread carrying the label.
    Pages through Gmail's 100-per-page response cap."""
    out = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "labelIds": [label_id], "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = svc.users().threads().list(**kwargs).execute()
        threads = resp.get("threads", []) or []
        out.extend(threads)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def get_thread_subject(svc, thread_id: str) -> str:
    """Pull the most-recent message's Subject from a thread. Used only in
    audit mode so the output is human-readable -- not called from
    unlabel/nuke since we only need IDs there."""
    try:
        t = svc.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        msgs = t.get("messages") or []
        if not msgs: return "(empty thread)"
        last = msgs[-1]
        headers = last.get("payload", {}).get("headers") or []
        subj = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(no subject)")
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
        return f"{subj[:80]}  --  {sender[:60]}"
    except Exception as e:
        return f"(metadata fetch failed: {e})"


def remove_label_from_thread(svc, thread_id: str, label_id: str) -> bool:
    if DRY_RUN:
        return True
    try:
        svc.users().threads().modify(
            userId="me", id=thread_id,
            body={"removeLabelIds": [label_id]},
        ).execute()
        return True
    except Exception as e:
        print(f"  [warn] unlabel failed for {thread_id}: {e}")
        return False


def delete_label(svc, label_id: str) -> bool:
    if DRY_RUN:
        return True
    try:
        svc.users().labels().delete(userId="me", id=label_id).execute()
        return True
    except Exception as e:
        print(f"  [warn] label delete failed: {e}")
        return False


def main():
    print(f"=== cleanup-auto-filed-label MODE={MODE} DRY_RUN={DRY_RUN} ===")
    svc = gmail_service()
    label = find_label(svc, LABEL_NAME)
    if not label:
        print(f"  Label '{LABEL_NAME}' does not exist -- nothing to do.")
        return
    label_id = label.get("id")
    print(f"  Label '{LABEL_NAME}' id={label_id}")
    threads = list_tagged_threads(svc, label_id)
    print(f"  {len(threads)} thread(s) currently carrying the label")

    if MODE == "audit":
        # Print thread subjects so Alex can eyeball what got tagged.
        for i, t in enumerate(threads[:50], 1):
            tid = t.get("id")
            subj = get_thread_subject(svc, tid)
            print(f"    {i:>3}. {tid}  {subj}")
        if len(threads) > 50:
            print(f"    ... and {len(threads) - 50} more (audit shows first 50)")
        return

    if MODE not in ("unlabel", "nuke"):
        print(f"  unknown MODE '{MODE}' -- choose audit|unlabel|nuke")
        sys.exit(2)

    # Unlabel every thread
    removed = 0
    for t in threads:
        if remove_label_from_thread(svc, t.get("id"), label_id):
            removed += 1
            if removed % 50 == 0:
                print(f"  ... {removed}/{len(threads)} unlabeled")
    print(f"  unlabeled {removed} thread(s)")

    if MODE == "nuke":
        ok = delete_label(svc, label_id)
        print(f"  label deletion: {'OK' if ok else 'FAILED'}")

    if DRY_RUN:
        print("\n  DRY_RUN was set -- no changes were actually made.")


if __name__ == "__main__":
    main()
