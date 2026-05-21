"""backfill-claude-usage.py — one-time historical backfill for the
Admin > Claude tab so Alex can see spend going back to each cron's
first run, not just from the day the migration landed.

Approach: query GitHub Actions for every successful run of each
Claude-using workflow, multiply (runs × median tokens-per-run) to
estimate input/output tokens + cost per run, and insert one synthetic
row per run into claude_usage_cloud.

Rows are clearly marked:
  - metadata.backfill = true
  - request_id = "backfill:<workflow_run_id>"  (idempotent dedup key)

So a re-run is safe (we never duplicate by request_id), and the UI
can render them at reduced opacity / with an "est" badge.

Netlify-side features (ai-chat, takeoff-ai-*, field-*, shorten-*)
are NOT backfilled — they fire per-user-action, not on a known
schedule. Their history only exists from the day the logger shipped.

Required env:
  GH_TOKEN                 (read access to alex-fusionelectric repos)
  SUPABASE_SERVICE_KEY
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SB_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
GH_REPO = "alex-fusionelectric/fusion-cloud-cron"

# (workflow_filename, feature_tag, model, est_input_per_run, est_output_per_run)
# Token estimates are conservative averages from a few sample real runs.
# Adjust here if a feature's actual usage proves higher.
WORKFLOWS = [
    ("sync-prequal-approvals.yml",    "prequal-parse",            "claude-haiku-4-5-20251001",   2000,  400),
    ("sync-prequal-approvals.yml",    "prequal-sbx-crossref",     "claude-haiku-4-5-20251001",   1500,  300),
    ("sync-bid-invitations.yml",      "bid-invitations-classify", "claude-haiku-4-5-20251001",   1200,  250),
    ("send-job-walk-invites.yml",     "job-walk-invite",          "claude-haiku-4-5-20251001",   1800,  600),
    ("send-weekly-status.yml",        "weekly-status-summary",    "claude-sonnet-4-6",          10000, 2000),
]

PRICING = {
    "claude-opus-4-7":            {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-haiku-4-5":           {"input": 0.80,  "output": 4.00},
}


def _gh(url: str) -> dict | list | None:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "fusion-backfill"}
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  [gh] HTTP {e.code}: {e.read()[:160]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [gh] {e}")
    return None


def fetch_workflow_runs(workflow_filename: str) -> list[dict]:
    """Pull every successful run for the workflow, paginating GH's
    100-per-page API. Cap at 30 pages = 3000 runs (plenty for any
    cron's lifetime to date)."""
    all_runs: list[dict] = []
    for page in range(1, 31):
        url = (f"https://api.github.com/repos/{GH_REPO}/actions/workflows/"
               f"{workflow_filename}/runs?status=success&per_page=100&page={page}")
        body = _gh(url)
        if not body:
            break
        runs = body.get("workflow_runs") or [] if isinstance(body, dict) else []
        if not runs:
            break
        all_runs.extend(runs)
        if len(runs) < 100:
            break
        time.sleep(0.2)  # be nice to the API
    return all_runs


def estimate_cost(model: str, inp: int, out: int) -> float:
    p = PRICING.get(model) or PRICING["claude-haiku-4-5-20251001"]
    return inp / 1_000_000 * p["input"] + out / 1_000_000 * p["output"]


def existing_backfill_ids(feature: str) -> set[str]:
    """Pull the set of request_id values we've already inserted for
    this feature so re-runs don't duplicate. Backfill ids always start
    with "backfill:" so the filter is cheap."""
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        return set()
    qs = (f"select=request_id&feature=eq.{urllib.parse.quote(feature)}"
          f"&request_id=like.backfill:*&limit=10000")
    url = f"{SB_URL}/rest/v1/claude_usage_cloud?{qs}"
    req = urllib.request.Request(url, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {row.get("request_id") for row in json.loads(r.read()) if row.get("request_id")}
    except Exception as e:  # noqa: BLE001
        print(f"  [sb-pre] {e}")
        return set()


def insert_rows(rows: list[dict]) -> int:
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY required")
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/claude_usage_cloud",
            data=json.dumps(chunk).encode(),
            method="POST",
            headers={
                "apikey": key, "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=30).read()
            total += len(chunk)
        except urllib.error.HTTPError as e:
            print(f"  [sb-ins] HTTP {e.code}: {e.read()[:160]!r}")
    return total


def main() -> int:
    if not (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip():
        raise SystemExit("SUPABASE_SERVICE_KEY required")

    grand_total = 0
    for workflow, feature, model, est_in, est_out in WORKFLOWS:
        print(f"\n=== {feature} (workflow: {workflow}) ===")
        runs = fetch_workflow_runs(workflow)
        if not runs:
            print("  no successful runs found")
            continue
        already = existing_backfill_ids(feature)
        print(f"  found {len(runs)} successful runs; {len(already)} already backfilled")

        cost = estimate_cost(model, est_in, est_out)
        rows: list[dict] = []
        for r in runs:
            run_id = r.get("id")
            if not run_id:
                continue
            rid = f"backfill:{run_id}"
            if rid in already:
                continue
            iso = r.get("run_started_at") or r.get("created_at") or r.get("updated_at")
            rows.append({
                "called_at":    iso,
                "feature":      feature,
                "model":        model,
                "input_tokens": est_in,
                "output_tokens": est_out,
                "cost_usd":     cost,
                "request_id":   rid,
                "metadata": {
                    "backfill":      True,
                    "workflow":      workflow,
                    "workflow_run":  run_id,
                    "estimated_tokens": True,
                },
            })

        n = insert_rows(rows)
        print(f"  inserted {n} synthetic rows (est: {est_in} in / {est_out} out per call · ${cost:.4f}/call)")
        grand_total += n

    print(f"\nTOTAL backfilled rows: {grand_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
