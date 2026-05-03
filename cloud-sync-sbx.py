"""Cloud-side SBX (Sacramento Builders Exchange / onlineplanservice.com)
scraper. Logs in with a Fusion Electric member account, pulls the JSON
grid endpoint for each mode (daily projects, daily addenda, bid results,
bidding), filters to the Bay-area counties Alex actually bids in, and
upserts to public.sbx_listings_cloud.

Required env:
  SUPABASE_SERVICE_KEY -- Supabase service-role key
  SBX_USERNAME         -- onlineplanservice.com login email
  SBX_PASSWORD         -- onlineplanservice.com password

Optional env:
  SBX_COUNTIES         -- comma-separated county allow-list (default:
                          Alameda,Contra Costa,Santa Clara,San Mateo)
  SBX_MODES            -- comma-separated modes to pull (default covers
                          dailyprojects,dailyaddenda,bidresultsprojects,
                          biddingprojects)
  SBX_PAGE_LIMIT       -- max pages per mode (default 20)

The site uses ASP.NET WebForms with ViewState. Login flow:
  GET /Login.aspx -> grab __VIEWSTATE + __VIEWSTATEGENERATOR
  POST /Login.aspx with username + password + Button1 -> session cookie
The grid data lives behind /ajax_grid_datasource.aspx?mode=X&page=N&pagesize=200.
"""

import json
import os
import re
import sys
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
LISTINGS_TABLE = "sbx_listings_cloud"
SESSION_KV_TABLE = "sbx_session_kv_cloud"
SBX_BASE = "https://login.onlineplanservice.com"
LOGIN_URL = f"{SBX_BASE}/Login.aspx?ReturnUrl=%2f"
GRID_URL = f"{SBX_BASE}/ajax_grid_datasource.aspx"

DEFAULT_COUNTIES = {"Alameda", "Contra Costa", "Santa Clara", "San Mateo"}
DEFAULT_MODES = ("dailyprojects", "dailyaddenda", "bidresultsprojects", "biddingprojects")
DEFAULT_PAGE_LIMIT = 20
PAGE_SIZE = 200  # site default is 50; 200 is the max it accepts cleanly

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


# --- Supabase REST helpers ---------------------------------------------------

def _service_key():
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return key


def _sb_request(method, path, *, body=None, headers_extra=None, timeout=30):
    key = _service_key()
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upsert_listings(rows, batch_size=200):
    if not rows:
        return 0
    sent = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        status, resp = _sb_request(
            "POST", LISTINGS_TABLE,
            body=chunk,
            headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if status not in (200, 201, 204):
            print(f"  [warn] upsert chunk {i}-{i+len(chunk)} HTTP {status}: {resp[:300]!r}")
            continue
        sent += len(chunk)
    return sent


def kv_upsert(key, value):
    body = [{"key": key, "value": value, "updated_at": datetime.utcnow().isoformat() + "Z"}]
    status, resp = _sb_request(
        "POST", SESSION_KV_TABLE, body=body,
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if status not in (200, 201, 204):
        print(f"  [warn] kv_upsert HTTP {status}: {resp[:200]!r}")


# --- SBX session -------------------------------------------------------------

def make_opener():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler(),
    )
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9")]
    return opener, cj


def find_form_value(html, name):
    m = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else ""


def sbx_login():
    user = (os.environ.get("SBX_USERNAME") or "").strip()
    pwd = os.environ.get("SBX_PASSWORD") or ""  # don't strip -- valid passwords can have whitespace
    if not (user and pwd):
        raise SystemExit("SBX_USERNAME and SBX_PASSWORD env vars required.")
    opener, cj = make_opener()

    r = opener.open(LOGIN_URL, timeout=30)
    html = r.read().decode("utf-8", errors="replace")
    viewstate = find_form_value(html, "__VIEWSTATE")
    viewstategen = find_form_value(html, "__VIEWSTATEGENERATOR")
    eventval = find_form_value(html, "__EVENTVALIDATION")

    form = urllib.parse.urlencode({
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": viewstategen,
        "__EVENTVALIDATION": eventval,
        "username": user,
        "password": pwd,
        "Button1": "Login",
    }).encode()
    req = urllib.request.Request(
        LOGIN_URL, data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    r = opener.open(req, timeout=30)
    body = r.read().decode("utf-8", errors="replace")

    # Detect successful auth: landing URL contains /projectgrid.aspx and the page has nav links.
    landed = r.url
    ok = "/projectgrid.aspx" in landed or "Logout" in body
    if not ok:
        # Failed auth typically returns the login page with an error label.
        err_match = re.search(r'<span[^>]*ErrorMessage[^>]*>([^<]+)</span>', body)
        msg = err_match.group(1).strip() if err_match else "(no error message in response)"
        raise SystemExit(f"SBX login failed for {user}: {msg}")
    print(f"SBX auth OK -- landed at {landed}")
    return opener


# --- Grid scrape -------------------------------------------------------------

def fetch_mode(opener, mode, *, page_limit, counties):
    """Yield raw project records for `mode`, filtered to `counties`."""
    out = []
    for page in range(1, page_limit + 1):
        url = f"{GRID_URL}?mode={mode}&page={page}&pagesize={PAGE_SIZE}"
        try:
            r = opener.open(url, timeout=45)
            data = json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            print(f"  [warn] {mode} page {page} fetch failed: {e}")
            break
        page_projects = data.get("Projects") or []
        if not page_projects:
            break
        kept = [p for p in page_projects if (p.get("County") or "").strip() in counties]
        out.extend(kept)
        total = data.get("Count") or 0
        print(f"  {mode} page {page:>2}: {len(page_projects)} fetched, {len(kept)} match counties (running total: {len(out)} / {total} in set)")
        # Stop early if we got fewer than a full page.
        if len(page_projects) < PAGE_SIZE:
            break
        # Polite throttle
        time.sleep(0.4)
    return out


def to_row(mode, p):
    """Map an SBX raw record to a Supabase row."""
    def _strip(v):
        return v.strip() if isinstance(v, str) else v

    def _date(v):
        s = _strip(v)
        if not s:
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except Exception:  # noqa: BLE001
                continue
        return None

    plannum = (_strip(p.get("opsplannum")) or "").strip()
    if not plannum:
        return None
    project_link = _strip(p.get("ProjectLink")) or ""
    full_link = f"{SBX_BASE}/{project_link.lstrip('/')}" if project_link else ""
    return {
        "id":               f"{mode}::{plannum}",
        "mode":             mode,
        "opsplannum":       plannum,
        "bxuniqplan":       _strip(p.get("bxuniqplan")),
        "project_name":     _strip(p.get("projectname")),
        "description":      _strip(p.get("description")),
        "county":           _strip(p.get("County")),
        "state":            _strip(p.get("state")),
        "location":         _strip(p.get("location")),
        "bid_date":         _date(p.get("biddate")),
        "date_received":    _date(p.get("datereceived")),
        "owner_agency":     _strip(p.get("owner")),
        "bid_package_id":   p.get("bidpackageid"),
        "project_link":     full_link,
        "doc_status":       p.get("DocStatus"),
        "total_bid_packages": p.get("TotalBidPackages"),
        "postponed":        bool(p.get("postponed") or False),
        "payload":          p,
        "generated_at":     datetime.utcnow().isoformat() + "Z",
        "updated_at":       datetime.utcnow().isoformat() + "Z",
    }


# --- Main --------------------------------------------------------------------

def main():
    counties_env = (os.environ.get("SBX_COUNTIES") or "").strip()
    counties = (
        {c.strip() for c in counties_env.split(",") if c.strip()}
        if counties_env else DEFAULT_COUNTIES
    )
    modes_env = (os.environ.get("SBX_MODES") or "").strip()
    modes = (
        tuple(m.strip() for m in modes_env.split(",") if m.strip())
        if modes_env else DEFAULT_MODES
    )
    page_limit = int(os.environ.get("SBX_PAGE_LIMIT") or DEFAULT_PAGE_LIMIT)

    print(f"SBX scrape: counties={sorted(counties)}, modes={modes}, page_limit={page_limit}")
    started = time.time()
    opener = sbx_login()

    all_rows = []
    per_mode_counts = {}
    for mode in modes:
        records = fetch_mode(opener, mode, page_limit=page_limit, counties=counties)
        rows = [r for r in (to_row(mode, p) for p in records) if r]
        per_mode_counts[mode] = len(rows)
        all_rows.extend(rows)
        # Polite throttle between modes
        time.sleep(0.5)

    print(f"\nTotal rows to upsert: {len(all_rows)}")
    for m, n in per_mode_counts.items():
        print(f"  {m:>22}: {n}")

    sent = upsert_listings(all_rows)
    print(f"Upserted {sent} row(s) into public.{LISTINGS_TABLE}.")

    kv_upsert("last_run", {
        "ts": datetime.utcnow().isoformat() + "Z",
        "rows": len(all_rows),
        "per_mode": per_mode_counts,
        "counties": sorted(counties),
        "elapsed_s": round(time.time() - started, 1),
    })


if __name__ == "__main__":
    main()
