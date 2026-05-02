"""Cloud-side BUDGET LIST parser: downloads BUDGET LIST.xlsm from a
SharePoint share URL (anonymous &download=1) and writes the 46-row
budget snapshot into public.budgets_cloud in Supabase.

Mirrors cloud-sync-bid-list.py's structure -- same fetch logic (cookie
jar + magic-byte sanity check), same Supabase auth resolution (env var
or service-key file), same truncate-and-insert pattern. The parse logic
itself is reused verbatim from convert-budget-list.py via importlib
shim, so any change there flows through here automatically.

USAGE
  python cloud-sync-budget-list.py \
      --url "https://...sharepoint.com/.../BUDGET%20LIST.xlsm?e=..." \
      --supabase-write

Optional --out <path> dumps the parsed payload to a JSON file for diff
validation against the local pipeline before cutover.
"""

import argparse
import http.cookiejar
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import openpyxl  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import convert_budget_list_compat  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "budgets_cloud"


def fetch_xlsm_bytes(share_url):
    """Same cookie-jar dance as cloud-sync-bid-list.py -- SharePoint
    redirects need cookies preserved across hops, default urllib doesn't,
    so an explicit opener with HTTPCookieProcessor is required. Magic-byte
    check makes failure modes (revoked link, tenant policy change, expired
    URL) loud instead of dumping HTML into openpyxl."""
    sep = "&" if "?" in share_url else "?"
    url = share_url + sep + "download=1"
    # URL scrubbed -- public-repo workflow logs (see cloud-sync-bid-list.py).
    print("Downloading SharePoint xlsm...")
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        urllib.request.HTTPRedirectHandler(),
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "*/*",
    })
    with opener.open(req, timeout=120) as resp:
        if resp.status != 200:
            raise SystemExit(f"download failed: HTTP {resp.status}")
        data = resp.read()
    if not data.startswith(b"PK"):
        head = data[:120].decode("utf-8", errors="replace")
        raise SystemExit(
            f"download returned non-xlsm content ({len(data):,} bytes, "
            f"starts with {head!r}). Likely the share link expired or "
            f"tenant policy changed."
        )
    print(f"  got {len(data):,} bytes ({len(data)/1024/1024:.2f} MB)")
    return data


def _resolve_service_key():
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if key:
        return key
    fallback = SCRIPTS_DIR.parent.parent / "fusion-bid-list" / "supabase-service-key.txt"
    if fallback.is_file():
        contents = fallback.read_text(encoding="utf-8").strip()
        if contents:
            return contents
    raise SystemExit(
        "no Supabase service key found. Set SUPABASE_SERVICE_KEY env var "
        f"or create {fallback}."
    )


def _project_to_row(project, generated_at):
    """Project a budget-list project dict (the convert-budget-list.py
    output shape) into a budgets_cloud row. Native columns = the ones
    the portal filters/sorts on; the full record lives in `payload` so
    schema additions are zero-migration."""
    return {
        "id":               project.get("id"),
        "job_number":       project.get("jobNumber") or None,
        "full_number":      project.get("fullNumber") or None,
        "project_name":     project.get("projectName") or None,
        "division":         project.get("division") or None,
        "project_manager":  project.get("projectManager") or None,
        "project_engineer": project.get("projectEngineer") or None,
        "contract_total":   project.get("contractTotal"),
        "total_budget":     project.get("totalBudget"),
        "est_total_cost":   project.get("estTotalCost"),
        "est_profit":       project.get("estProfit"),
        "est_profit_pct":   project.get("estProfitPct"),
        "health":           project.get("health"),
        "pct_complete":     project.get("pctComplete"),
        "payload":          project,
        "generated_at":     generated_at,
        "updated_at":       generated_at,
    }


def write_to_supabase(payload, *, batch_size=200):
    key = _resolve_service_key()
    api = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }
    projects = payload.get("projects", [])
    aggregates = payload.get("aggregates", {})
    generated_at = payload.get("generatedAt")
    rows = [_project_to_row(p, generated_at) for p in projects]

    print(f"Clearing existing rows in public.{SUPABASE_TABLE}...")
    req = urllib.request.Request(api + "?id=not.is.null", method="DELETE", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise SystemExit(f"DELETE failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"DELETE failed: HTTP {e.code} {body}")

    print(f"Inserting {len(rows)} row(s) in batches of {batch_size}...")
    sent = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        body = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(api, method="POST", headers=headers, data=body)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status not in (200, 201, 204):
                    raise SystemExit(f"POST failed mid-batch: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise SystemExit(f"POST failed at row {i}: HTTP {e.code} {err}")
        sent += len(chunk)
        print(f"  ... {sent}/{len(rows)}")
    print(f"Wrote {sent} project row(s) to public.{SUPABASE_TABLE}.")

    # Aggregates: persist as a single meta row so the portal can fetch
    # totals/health/division counts without re-summing client-side.
    # Same table -- meta row is identified by id="__meta__" and lives
    # alongside project rows; anyone can ignore it via id != '__meta__'.
    meta_row = {
        "id":           "__meta__",
        "job_number":   None,
        "project_name": "(aggregates)",
        "payload":      {"aggregates": aggregates, "count": payload.get("count"), "generatedAt": generated_at},
        "generated_at": generated_at,
        "updated_at":   generated_at,
    }
    body = json.dumps([meta_row]).encode("utf-8")
    req = urllib.request.Request(api, method="POST", headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201, 204):
                raise SystemExit(f"POST meta failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"POST meta failed: HTTP {e.code} {err}")
    print("Wrote aggregates meta row (id='__meta__').")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="OneDrive/SharePoint share link for BUDGET LIST.xlsm.")
    ap.add_argument("--out", default=None,
                    help="Optional: write parsed payload to this JSON path for diff validation.")
    ap.add_argument("--supabase-write", action="store_true",
                    help="Truncate + insert all project rows + aggregates meta into public.budgets_cloud.")
    args = ap.parse_args()

    xlsm = fetch_xlsm_bytes(args.url)
    wb = openpyxl.load_workbook(io.BytesIO(xlsm), data_only=True)
    payload = convert_budget_list_compat.parse_workbook(wb, source_label="cloud:sharepoint-share-url")
    print(f"Parsed {len(payload['projects'])} budget records.")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote payload to {out_path}")

    if args.supabase_write:
        write_to_supabase(payload)

    if not args.out and not args.supabase_write:
        print("[warn] no output flag set (--out or --supabase-write). "
              "Parse succeeded but result was discarded.", file=sys.stderr)


if __name__ == "__main__":
    main()
