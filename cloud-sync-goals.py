"""Cloud-side goals parser: downloads BID LIST.xlsm from a SharePoint
share URL (anonymous &download=1) and writes the company goals (TRACKING
sheet) to public.goals_cloud in Supabase.

Same pattern as cloud-sync-bid-list.py / cloud-sync-budget-list.py /
cloud-sync-project-list.py. Reuses the existing BID_LIST_URL secret in
GitHub Actions since the goals live in the same xlsm.

USAGE
  python cloud-sync-goals.py --url "https://...sharepoint.com/.../BID%20LIST.xlsm?e=..." --supabase-write
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

import convert_tracking_compat  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "goals_cloud"


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
    with opener.open(req, timeout=120) as resp:
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
    raise SystemExit(f"no Supabase service key found. Set SUPABASE_SERVICE_KEY or create {fallback}.")


def write_to_supabase(payload):
    """One row per year in goals_cloud, keyed on year. Upsert via
    Prefer: resolution=merge-duplicates so a re-run for the same year
    overwrites cleanly without DELETE first."""
    key = _resolve_service_key()
    api = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    year = str(payload.get("year") or datetime.utcnow().year)
    generated_at = payload.get("generatedAt")
    row = {
        "year": year,
        "payload": payload,
        "generated_at": generated_at,
        "updated_at": generated_at,
    }
    print(f"Upserting goals for year {year} into public.{SUPABASE_TABLE}...")
    body = json.dumps([row]).encode("utf-8")
    req = urllib.request.Request(api, method="POST", headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201, 204):
                raise SystemExit(f"upsert failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"upsert failed: HTTP {e.code} {body_text}")
    print(f"Wrote goals for year {year} to public.{SUPABASE_TABLE}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="OneDrive/SharePoint share link for BID LIST.xlsm.")
    ap.add_argument("--out", default=None, help="Optional: write parsed payload to this JSON path for diff validation.")
    ap.add_argument("--supabase-write", action="store_true", help="Upsert goals into public.goals_cloud.")
    args = ap.parse_args()

    xlsm = fetch_xlsm_bytes(args.url)
    wb = openpyxl.load_workbook(io.BytesIO(xlsm), data_only=True)
    payload = convert_tracking_compat.parse_workbook(wb, source_label="cloud:sharepoint-share-url")
    year = payload.get("year")
    print(f"Parsed goals for year {year}, divisions: {list(payload.get('goals', {}).get(str(year), {}).keys())}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote payload to {out_path}")

    if args.supabase_write:
        write_to_supabase(payload)


if __name__ == "__main__":
    main()
