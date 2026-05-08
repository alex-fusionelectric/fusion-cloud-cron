#!/usr/bin/env python3
"""geocode-project-addresses.py

Resolves any project_locations_cloud rows where lat IS NULL using the free
Nominatim (OpenStreetMap) geocoder. Runs nightly via GitHub Actions; also
fine to invoke ad-hoc via workflow_dispatch when Alex bulk-adds addresses.

Nominatim usage policy:
  - 1 req/sec hard cap (we sleep 1.1s between calls to be safe)
  - Identifying User-Agent required ("FusionElectric-FieldPanel/1.0
    contact: alex@fusionelectric-inc.com")
  - https://operations.osmfoundation.org/policies/nominatim/

For rows that fail to geocode (returns no result), we stamp
geocode_status='not_found' so the dashboard can flag them for manual
correction without retrying every run.

Required env: SUPABASE_SERVICE_KEY
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

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "FusionElectric-FieldPanel/1.0 (alex@fusionelectric-inc.com)"
SLEEP_BETWEEN = 1.1  # seconds — Nominatim's 1-req/sec rate limit

# Re-geocode rows older than this (stale → maybe the address was edited)
REGEOCODE_AFTER_DAYS = 30


def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb(method: str, path: str, body=None, extra=None, timeout=30):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": _service_key(),
        "Authorization": f"Bearer {_service_key()}",
        "content-type": "application/json",
    }
    if extra:
        headers.update(extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def fetch_pending() -> list[dict]:
    """Rows that need a (re-)geocode: never geocoded, OR stale OR errored."""
    cutoff = datetime.now(timezone.utc).timestamp() - REGEOCODE_AFTER_DAYS * 86400
    iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")
    qs = (
        "select=full_number,address,city,state,geocoded_at,geocode_status"
        f"&or=(lat.is.null,geocoded_at.lt.{urllib.parse.quote(iso)},geocode_status.eq.error)"
        "&order=full_number"
    )
    st, body = _sb("GET", f"project_locations_cloud?{qs}")
    if st != 200:
        print(f"[err] fetch HTTP {st}: {body[:200]!r}", file=sys.stderr)
        return []
    return json.loads(body)


def geocode(addr: str, city: str | None, state: str | None) -> dict:
    """Single Nominatim request. Returns {'lat','lng','status'} (no exceptions)."""
    parts = [addr]
    if city: parts.append(city)
    if state: parts.append(state)
    parts.append("USA")
    q = ", ".join(parts)
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode({'q': q, 'format': 'json', 'limit': '1', 'addressdetails': '0'})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"status": f"error: {e}"}
    if not data:
        return {"status": "not_found"}
    try:
        return {
            "lat": float(data[0]["lat"]),
            "lng": float(data[0]["lon"]),
            "status": "ok",
        }
    except (KeyError, ValueError) as e:
        return {"status": f"error: {e}"}


def update_row(full_number: str, result: dict) -> None:
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {
        "lat": result.get("lat"),
        "lng": result.get("lng"),
        "geocode_status": result.get("status"),
        "geocoded_at": iso,
        "updated_at": iso,
    }
    st, resp = _sb(
        "PATCH",
        f"project_locations_cloud?full_number=eq.{urllib.parse.quote(full_number, safe='')}",
        body=body,
    )
    if st not in (200, 204):
        print(f"  [warn] update HTTP {st}: {resp[:200]!r}", file=sys.stderr)


def main():
    pending = fetch_pending()
    print(f"=== geocode-project-addresses ({len(pending)} pending row(s)) ===")
    for i, row in enumerate(pending, 1):
        addr = (row.get("address") or "").strip()
        if not addr:
            update_row(row["full_number"], {"status": "not_found"})
            continue
        print(f"[{i:3d}/{len(pending)}] {row['full_number']:14s} {addr[:60]}")
        result = geocode(addr, row.get("city"), row.get("state"))
        update_row(row["full_number"], result)
        s = result.get("status", "")
        print(f"    -> {s}{' ' + str(result.get('lat'))+','+str(result.get('lng')) if result.get('lat') else ''}")
        time.sleep(SLEEP_BETWEEN)
    print("\nDone.")


if __name__ == "__main__":
    main()
