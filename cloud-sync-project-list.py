"""Cloud-side PROJECT LIST parser: downloads PROJECT LIST.xlsm from a
SharePoint share URL (anonymous &download=1) and writes the project +
change-orders snapshot into public.projects_cloud in Supabase.

Same pattern as cloud-sync-bid-list.py and cloud-sync-budget-list.py.
The actual parse logic lives in the shared convert-project-list.py via
importlib shim, so any update to the local parser propagates here
automatically.

USAGE
  python cloud-sync-project-list.py \
      --url "https://...sharepoint.com/.../PROJECT%20LIST.xlsm?e=..." \
      --supabase-write
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

import convert_project_list_compat  # noqa: E402

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SUPABASE_TABLE = "projects_cloud"


def fetch_xlsm_bytes(share_url):
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
    with opener.open(req, timeout=180) as resp:
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
    """Project a project dict (convert-project-list.py output shape) into
    a projects_cloud row. Native columns = the ones PM Panel filters/sorts
    on; full record lives in `payload` so schema additions are
    zero-migration."""
    return {
        "id":               project.get("id"),
        "est_number":       project.get("estNumber") or None,
        "full_number":      project.get("fullNumber") or None,
        "project_name":     project.get("projectName") or None,
        "division":         project.get("division") or None,
        "project_manager":  project.get("projectManager") or None,
        "project_engineer": project.get("projectEngineer") or None,
        "project_type":     project.get("projectType") or None,
        "client_gc":        project.get("clientGc") or None,
        "award_date":       project.get("awardDate"),
        "start_date":       project.get("startDate"),
        "end_date":         project.get("endDate"),
        "bid_amount":       project.get("bidAmount"),
        "awarded_amount":   project.get("awardedAmount"),
        "status":           project.get("status") or None,
        "outcome":          project.get("outcome") or None,
        "payload":          project,
        "generated_at":     generated_at,
        "updated_at":       generated_at,
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

    bids = payload.get("bids", [])
    change_orders = payload.get("changeOrders", {})
    generated_at = payload.get("generatedAt")
    rows = [_project_to_row(b, generated_at) for b in bids]

    print(f"Upserting {len(rows)} project row(s) in batches of {batch_size}...")
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
    print(f"Wrote {sent} project row(s) to public.{SUPABASE_TABLE}.")

    # Meta row — upserted before stale-purge so it's always current.
    meta_row = {
        "id":           "__meta__",
        "project_name": "(change orders)",
        "payload":      {
            "changeOrders": change_orders,
            "count":        payload.get("count"),
            "coCount":      payload.get("coCount"),
            "generatedAt":  generated_at,
        },
        "generated_at": generated_at,
        "updated_at":   generated_at,
    }
    body = json.dumps([meta_row]).encode("utf-8")
    req = urllib.request.Request(api, method="POST", headers=headers_upsert, data=body)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 201, 204):
                raise SystemExit(f"POST meta failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"POST meta failed: HTTP {e.code} {err}")
    co_count = payload.get("coCount", 0)
    print(f"Wrote aggregates meta row (id='__meta__', {co_count} change orders).")

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
    ap.add_argument("--url", required=True, help="OneDrive/SharePoint share link for PROJECT LIST.xlsm.")
    ap.add_argument("--out", default=None,
                    help="Optional: write parsed payload to this JSON path for diff validation.")
    ap.add_argument("--supabase-write", action="store_true",
                    help="Truncate + insert all project rows + change-order meta into public.projects_cloud.")
    args = ap.parse_args()

    xlsm = fetch_xlsm_bytes(args.url)
    wb = openpyxl.load_workbook(io.BytesIO(xlsm), read_only=False, data_only=True)
    payload = convert_project_list_compat.parse_workbook(wb, source_label="cloud:sharepoint-share-url")
    print(
        f"Parsed {len(payload['bids'])} project bids and {payload['coCount']} change orders "
        f"across {len(payload['changeOrders'])} projects."
    )

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
