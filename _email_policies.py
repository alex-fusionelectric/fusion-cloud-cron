"""_email_policies.py -- Python sibling of netlify/functions/_email_policies.js.

Every cron that sends outbound email reads its recipient list from
public.email_policies_cloud via get_recipients_for() instead of hardcoding
addresses. Lets the /admin/ Email Center tab flip a checkbox to change
who gets pinged without re-deploying the cron.

Safety guarantees:
  - If the table doesn't exist yet (Alex hasn't run the DDL), fall back to
    the per-policy default below so the cron keeps working in degraded mode.
  - If a row is disabled (enabled=false), return an empty list so the
    caller can short-circuit and skip the send entirely.
  - On any error, return the default rather than crashing.

Service-role read is required because RLS hides the table from anon.
Uses the SUPABASE_SERVICE_KEY env var that every other cron already sets.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"

DEFAULT_RECIPIENTS = {
    "bid_setup_complete": [
        "alex@fusionelectric-inc.com",
        "gabriel.toler@fusionelectric-inc.com",
    ],
    "addenda_alert": [
        "alex@fusionelectric-inc.com",
        "gabriel.toler@fusionelectric-inc.com",
    ],
    "daily_changes": ["alex@fusionelectric-inc.com"],
    "weekly_digest": ["alex@fusionelectric-inc.com"],
}


def _service_key() -> str:
    return (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()


def _fetch_policy(policy_key: str) -> dict | None:
    key = _service_key()
    if not key:
        return None
    url = (f"{SUPABASE_URL}/rest/v1/email_policies_cloud"
           f"?policy_key=eq.{urllib.parse.quote(policy_key)}"
           f"&select=enabled,recipients,last_sent_at")
    req = urllib.request.Request(url, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read().decode("utf-8"))
            return rows[0] if rows else None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None


def get_recipients_for(policy_key: str) -> list[str]:
    """Return the recipient list to actually use, after applying the
    enabled flag.

      Policy present + enabled  -> policy.recipients
      Policy present + disabled -> []  (caller should skip the send)
      Policy missing / err      -> DEFAULT_RECIPIENTS[policy_key] or []
    """
    row = _fetch_policy(policy_key)
    if row is not None:
        if not row.get("enabled"):
            return []
        recipients = row.get("recipients") or []
        if isinstance(recipients, list):
            return [e for e in recipients
                    if isinstance(e, str) and "@" in e]
    return list(DEFAULT_RECIPIENTS.get(policy_key, []))


def record_send(policy_key: str, recipients: list[str]) -> None:
    """Stamp last_sent_at / last_recipients / last_send_count on the policy
    row so the Email Center UI can show 'last sent X to N recipients'.
    Fire-and-forget -- send success is independent of audit write success."""
    key = _service_key()
    if not key:
        return
    url = (f"{SUPABASE_URL}/rest/v1/email_policies_cloud"
           f"?policy_key=eq.{urllib.parse.quote(policy_key)}")
    body = json.dumps({
        "last_sent_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_recipients": recipients,
        "last_send_count": len(recipients),
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass  # non-fatal
