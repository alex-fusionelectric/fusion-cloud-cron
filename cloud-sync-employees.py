"""Cloud-side Employees parser. Downloads PROJECT LIST.xlsm from a
SharePoint share URL, reads the hidden EMPLOYEES sheet, writes to
public.employees_cloud in Supabase.

Reuses PROJECT_LIST_URL secret in GitHub Actions (same xlsm).

USAGE
  python cloud-sync-employees.py --url "https://...sharepoint.com/.../PROJECT%20LIST.xlsm?e=..." --supabase-write
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

import convert_employees_compat  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "employees_cloud"


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
        raise SystemExit(f"non-xlsm content ({len(data):,} bytes, starts with {head!r})")
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
    raise SystemExit("no Supabase service key found.")


def _emp_to_row(emp, generated_at):
    return {
        "id":           emp.get("id"),
        "employee_id":  emp.get("employeeId"),
        "name":         emp.get("name"),
        "name_upper":   emp.get("nameUpper"),
        "division":     emp.get("division"),
        "title":        emp.get("title"),
        "home_local":   emp.get("homeLocal"),
        "active":       str(emp.get("active") or "true").lower() == "true",
        "payload":      emp,
        "generated_at": generated_at,
        "updated_at":   generated_at,
    }


def write_to_supabase(payload, *, batch_size=200):
    key = _resolve_service_key()
    api = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    employees = payload.get("employees", [])
    generated_at = payload.get("generatedAt")
    rows = [_emp_to_row(e, generated_at) for e in employees if e.get("id")]

    print(f"Clearing existing rows in public.{SUPABASE_TABLE}...")
    req = urllib.request.Request(api + "?id=not.is.null", method="DELETE", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise SystemExit(f"DELETE failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"DELETE failed: HTTP {e.code} {body}")

    print(f"Inserting {len(rows)} employee row(s) in batches of {batch_size}...")
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
    print(f"Wrote {sent} employee row(s) to public.{SUPABASE_TABLE}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="OneDrive/SharePoint share link for PROJECT LIST.xlsm.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--supabase-write", action="store_true")
    args = ap.parse_args()

    xlsm = fetch_xlsm_bytes(args.url)
    wb = openpyxl.load_workbook(io.BytesIO(xlsm), data_only=True)
    payload = convert_employees_compat.parse_workbook(wb, source_label="cloud:sharepoint-share-url")
    print(f"Parsed {len(payload['employees'])} employees.")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote payload to {out_path}")

    if args.supabase_write:
        write_to_supabase(payload)


if __name__ == "__main__":
    main()
