"""Fire-and-forget Claude usage logger for the cron scripts.

Every script that calls Anthropic should also call log_claude_call()
with the response's usage block + a short feature tag so the Admin >
Claude tab can surface token spend + cost.

Designed to never break the caller: logging failures swallow into a
stderr warning. Requires SUPABASE_SERVICE_KEY in env.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
TABLE = "claude_usage_cloud"

# Per-1M-token pricing (USD) — keep in sync with
# fusion-bid-list/netlify/functions/_claude_log.js. Standard tier only.
PRICING = {
    "claude-opus-4-7":            {"input": 15.00, "output": 75.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-5":          {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-haiku-4-5":           {"input": 0.80,  "output": 4.00},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING.get(model) or PRICING["claude-haiku-4-5-20251001"]
    return (input_tokens or 0) / 1_000_000 * p["input"] \
         + (output_tokens or 0) / 1_000_000 * p["output"]


def log_claude_call(
    *,
    feature: str,
    model: str,
    usage: dict | None,
    est_number: str | None = None,
    user_email: str | None = None,
    request_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Insert one row into claude_usage_cloud. Silently no-ops if
    SUPABASE_SERVICE_KEY is unset or the table is missing."""
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        return
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cost = estimate_cost(model, input_tokens, output_tokens)
    payload = {
        "feature":       str(feature or "unknown")[:80],
        "est_number":    (str(est_number)[:40] if est_number else None),
        "model":         str(model or "unknown")[:80],
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "cost_usd":      cost,
        "request_id":    request_id,
        "user_email":    user_email,
        "metadata":      metadata,
    }
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as e:
        # Table-missing (404) is the common case before the migration
        # has been run. Don't spam stderr — only complain on real errors.
        if e.code not in (404,):
            print(f"  [claude-log] HTTP {e.code}: {e.read()[:160]!r}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [claude-log] {type(e).__name__}: {e}", file=sys.stderr)
