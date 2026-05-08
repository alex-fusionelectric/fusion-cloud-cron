"""Cloud-side Jobs Archive parser: downloads BUDGET LIST.xlsm from a
SharePoint share URL and writes the historical job archive
(JOBS sheet + cost rollups) to public.jobs_archive_cloud in Supabase.

Reuses the existing BUDGET_LIST_URL secret in GitHub Actions.

USAGE
  python cloud-sync-jobs-archive.py --url "https://...sharepoint.com/.../BUDGET%20LIST.xlsm?e=..." --supabase-write
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

import convert_jobs_archive_compat  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "jobs_archive_cloud"


def fetch_xlsm_bytes(share_url):
    sep = "&" if "?" in share_url else "?"
    url = share_url + sep + "download=1"
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
    with opener.open(req, timeout=180) as resp:
        if resp.status != 200:
            raise SystemExit(f"download failed: HTTP {resp.status}")
        data = resp.read()
    if not data.startswith(b"PK"):
        head = data[:120].decode("utf-8", errors="replace")
        raise SystemExit(f"download returned non-xlsm content ({len(data):,} bytes, starts with {head!r})")
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
    raise SystemExit(f"no Supabase service key found.")


def _job_to_row(job, generated_at):
    return {
        "id":              job.get("jobId") or job.get("id") or "",
        "job_id":          job.get("jobId"),
        "description":     job.get("description"),
        "project_manager": job.get("projectManager"),
        "project_engineer": job.get("projectEngineer"),
        "geo_area":        job.get("geoArea"),
        "year":            job.get("year"),
        "is_active":       job.get("isActive"),
        "status_label":    job.get("statusLabel"),
        "original_contract": job.get("originalContract"),
        "total_actual_cost": job.get("totalActualCost"),
        "est_profit":      job.get("estProfit"),
        "est_profit_pct":  job.get("estProfitPct"),
        "profit_data_quality": job.get("profitDataQuality"),
        "payload":         job,
        "generated_at":    generated_at,
        "updated_at":      generated_at,
    }


def write_to_supabase(payload, *, batch_size=200):
    key = _resolve_service_key()
    api = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers_upsert = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    headers_delete = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }
    jobs = payload.get("jobs", [])
    aggregates = payload.get("aggregates", {})
    generated_at = payload.get("generatedAt")
    rows = [_job_to_row(j, generated_at) for j in jobs]
    rows = [r for r in rows if r.get("id")]

    print(f"Upserting {len(rows)} job row(s) in batches of {batch_size}...")
    sent = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        body = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(api, method="POST", headers=headers_upsert, data=body)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status not in (200, 201, 204):
                    raise SystemExit(f"POST failed mid-batch: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise SystemExit(f"POST failed at row {i}: HTTP {e.code} {err}")
        sent += len(chunk)
        print(f"  ... {sent}/{len(rows)}")
    print(f"Wrote {sent} job row(s) to public.{SUPABASE_TABLE}.")

    # Aggregates meta row — upserted before stale-purge so it's always current.
    meta_row = {
        "id":           "__meta__",
        "job_id":       None,
        "description":  "(aggregates)",
        "payload":      {"aggregates": aggregates, "count": payload.get("count"), "generatedAt": generated_at},
        "generated_at": generated_at,
        "updated_at":   generated_at,
    }
    body = json.dumps([meta_row]).encode("utf-8")
    req = urllib.request.Request(api, method="POST", headers=headers_upsert, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201, 204):
                raise SystemExit(f"POST meta failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"POST meta failed: HTTP {e.code} {err}")
    print("Wrote aggregates meta row (id='__meta__').")

    # Purge rows from previous runs not present in this parse.
    stale_url = api + "?updated_at=neq." + urllib.parse.quote(generated_at, safe="")
    req = urllib.request.Request(stale_url, method="DELETE", headers=headers_delete)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise SystemExit(f"DELETE stale failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"DELETE stale failed: HTTP {e.code} {body}")
    print(f"Purged stale rows (updated_at != {generated_at}).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="OneDrive/SharePoint share link for BUDGET LIST.xlsm.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--supabase-write", action="store_true")
    args = ap.parse_args()

    xlsm = fetch_xlsm_bytes(args.url)
    wb = openpyxl.load_workbook(io.BytesIO(xlsm), data_only=True)
    payload = convert_jobs_archive_compat.parse_workbook(wb, source_label="cloud:sharepoint-share-url")
    print(f"Parsed {len(payload['jobs'])} archived jobs.")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote payload to {out_path}")

    if args.supabase_write:
        write_to_supabase(payload)


if __name__ == "__main__":
    main()
