"""delete-sac-labels.py — one-shot: delete the SAC EST# labels wrongly
created under ESTIMATING/CURRENT BIDS/ in Alex's Gmail.

Only BAY bids should have labels in Alex's inbox. SAC bids are Austin's.

Run once, then delete this script.
Required env: GMAIL_TOKEN_JSON
"""
import json, os, sys

try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
except ImportError as e:
    print(f"[error] {e}", file=sys.stderr); sys.exit(2)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# SAC labels identified 2026-05-05 — labelId → EST#
SAC_LABELS = {
    "Label_3602": "26-082",
    "Label_3600": "26-152",
    "Label_3616": "26-183",
    "Label_3598": "26-193",
    "Label_3608": "26-197",
    "Label_3604": "26-200",
    "Label_3606": "26-203",
    "Label_3609": "26-206",
    "Label_3603": "26-208",
    "Label_3613": "26-214",
    "Label_3615": "26-217",
    "Label_3607": "26-218",
    "Label_3601": "26-219",
    "Label_3611": "26-220",
    "Label_3612": "26-225",
    "Label_3614": "26-226",
    "Label_3605": "26-233",
    "Label_3610": "26-238",
}

raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
if not raw:
    raise SystemExit("GMAIL_TOKEN_JSON env var required.")
creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
if not creds.valid and creds.refresh_token:
    creds.refresh(Request())
svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

deleted = 0
for label_id, est in SAC_LABELS.items():
    try:
        svc.users().labels().delete(userId="me", id=label_id).execute()
        print(f"  deleted  {label_id}  ({est})")
        deleted += 1
    except Exception as e:
        print(f"  [warn]   {label_id}  ({est})  {e}")

print(f"\nDone: {deleted}/{len(SAC_LABELS)} SAC labels deleted.")
