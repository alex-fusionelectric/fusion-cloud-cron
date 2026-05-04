"""Cloud-side SBX project-detail scraper.

For each active SBX listing (mode in biddingprojects/dailyprojects, future
bid_date), fetch the project detail page (filter.aspx?BidPackageID=N) and
extract:
  - bidder / plan-holder list (companies who have downloaded docs)
  - addenda count + per-addendum metadata
  - owner contact info
  - extended project description
  - bid date+time, pre-bid meeting, bid security, estimated value, duration

Writes to:
  sbx_project_details_cloud  -- one row per opsplannum
  sbx_plan_holders_cloud     -- one row per (opsplannum, gc_name)

Required env: SUPABASE_SERVICE_KEY, SBX_USERNAME, SBX_PASSWORD
Optional env:
  SBX_DETAIL_LIMIT       -- max projects to fetch per run (default 60)
  SBX_DETAIL_DISCOVERY   -- set "1" to capture raw_html for inspection (default "1"
                            on first deploy; switch to "0" once parsers are stable)
  SBX_DETAIL_MIN_INTERVAL_MIN -- skip projects fetched within this many minutes
                                 (default 25; pairs with 30-min cron)
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
from datetime import datetime, timedelta, timezone
from html import unescape

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SBX_BASE = "https://login.onlineplanservice.com"
LOGIN_URL = f"{SBX_BASE}/Login.aspx?ReturnUrl=%2f"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

DEFAULT_LIMIT = 60
DEFAULT_MIN_INTERVAL_MIN = 25


# --- Supabase REST helpers ---------------------------------------------------

def _service_key():
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return key


def _sb(method, path, *, body=None, extra_headers=None, timeout=30):
    key = _service_key()
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upsert(table, rows, *, batch=200):
    if not rows:
        return 0
    sent = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        st, _ = _sb("POST", table, body=chunk,
                    extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        if st in (200, 201, 204):
            sent += len(chunk)
        else:
            print(f"  [warn] upsert {table} chunk failed HTTP {st}")
    return sent


def fetch_setup_listings(limit):
    """Only scrape detail pages for opsplannums that Alex has actually SET UP
    via the SBX Watchlist's "Setup Test Bid" button. These live in
    public.prebid_bids_cloud with a non-null sbx_id ("{mode}::{opsplannum}")
    or an sbx_listing jsonb with opsplannum. We then look up the matching
    sbx_listings_cloud row to get bid_package_id (which is what filter.aspx
    needs).
    """
    st, body = _sb("GET", "prebid_bids_cloud?select=id,sbx_id,sbx_listing,bid_due_date,project_name&order=created_at.desc")
    if st != 200:
        raise SystemExit(f"failed to fetch prebid_bids_cloud: HTTP {st}")
    prebids = json.loads(body)

    # Extract opsplannums + map to project name fallback
    plannum_set = []
    name_by_pn = {}
    for r in prebids:
        sbx_id = (r.get("sbx_id") or "").strip()
        pn = ""
        if "::" in sbx_id:
            pn = sbx_id.split("::", 1)[1].strip()
        else:
            sl = r.get("sbx_listing") or {}
            pn = (sl.get("opsplannum") or "").strip()
        if not pn:
            continue
        if pn not in plannum_set:
            plannum_set.append(pn)
            name_by_pn[pn] = r.get("project_name")
    if not plannum_set:
        return []

    # Look up each in sbx_listings_cloud to get bid_package_id. Take the most
    # recent row per opsplannum across all modes (mode doesn't matter here --
    # all modes carry the same bid_package_id for a given project).
    in_clause = ",".join(plannum_set)
    q = (f"sbx_listings_cloud?select=opsplannum,bid_package_id,project_link,project_name,bid_date,county,mode"
         f"&opsplannum=in.({in_clause})&order=updated_at.desc")
    st, body = _sb("GET", q)
    if st != 200:
        raise SystemExit(f"failed to fetch sbx_listings_cloud: HTTP {st}")
    rows = json.loads(body)
    seen = {}
    for r in rows:
        # First (most-recent) wins
        seen.setdefault(r["opsplannum"], r)
    # Order by bid_date ascending so the most-imminent bids get refreshed first
    out = list(seen.values())
    # For prebid rows that exist but have no SBX listing match (e.g. bid invitation
    # source), fall back to a stub with whatever data we have. Skip if no BPID --
    # without bid_package_id we can't fetch the detail page.
    out.sort(key=lambda x: (x.get("bid_date") or "9999-12-31"))
    return out[:limit]


def fetch_recent_detail_fetches():
    """Return {opsplannum: fetched_at_iso} so we can skip ones recently scanned."""
    st, body = _sb("GET", "sbx_project_details_cloud?select=opsplannum,fetched_at")
    if st != 200:
        return {}
    return {r["opsplannum"]: r["fetched_at"] for r in json.loads(body)}


# --- SBX session -------------------------------------------------------------

def make_opener():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler(),
    )
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9")]
    return opener


def find_form_value(html, name):
    m = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else ""


def sbx_login():
    user = (os.environ.get("SBX_USERNAME") or "").strip()
    pwd = os.environ.get("SBX_PASSWORD") or ""
    if not (user and pwd):
        raise SystemExit("SBX_USERNAME and SBX_PASSWORD env vars required.")
    opener = make_opener()
    r = opener.open(LOGIN_URL, timeout=30)
    html = r.read().decode("utf-8", errors="replace")
    form = urllib.parse.urlencode({
        "__VIEWSTATE": find_form_value(html, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": find_form_value(html, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": find_form_value(html, "__EVENTVALIDATION"),
        "username": user,
        "password": pwd,
        "Button1": "Login",
    }).encode()
    req = urllib.request.Request(LOGIN_URL, data=form, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = opener.open(req, timeout=30)
    body = r.read().decode("utf-8", errors="replace")
    if "/projectgrid.aspx" not in r.url and "Logout" not in body:
        raise SystemExit("SBX login failed")
    print(f"SBX auth OK -- landed at {r.url}")
    return opener


# --- Detail page fetch + parse ----------------------------------------------

def normalize_gc(s):
    if not s:
        return ""
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fetch_detail(opener, bid_package_id, opsplannum):
    """Fetch the main filter.aspx project detail page. Returns (status, html)."""
    short = opsplannum.replace("COPS", "")
    url = (f"{SBX_BASE}/filter.aspx?"
           f"BidPackageID={bid_package_id}&projectnum={opsplannum}"
           f"&bx=COPS&bxup={short}")
    try:
        r = opener.open(url, timeout=30)
        return r.status, r.read().decode("utf-8", errors="replace"), url
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if e.fp else "", url
    except urllib.error.URLError as e:
        return 0, f"<urlerror>{e}</urlerror>", url


def try_planholder_endpoints(opener, bid_package_id, opsplannum):
    """SBX has a few candidate URL patterns for the plan holder list. Try
    each in order; capture whichever returns a sensible response. We'll
    keep the first non-redirect HTML body that looks like a list of
    companies."""
    short = opsplannum.replace("COPS", "")
    candidates = [
        f"{SBX_BASE}/planholderlist.aspx?BidPackageID={bid_package_id}",
        f"{SBX_BASE}/PlanHolderList.aspx?BidPackageID={bid_package_id}",
        f"{SBX_BASE}/planholder.aspx?BidPackageID={bid_package_id}",
        f"{SBX_BASE}/bidderlist.aspx?BidPackageID={bid_package_id}",
        f"{SBX_BASE}/bidders.aspx?BidPackageID={bid_package_id}&projectnum={opsplannum}",
        f"{SBX_BASE}/registered.aspx?BidPackageID={bid_package_id}",
        f"{SBX_BASE}/Public/PlanHolderList.aspx?BidPackageID={bid_package_id}",
    ]
    for url in candidates:
        try:
            r = opener.open(url, timeout=20)
            html = r.read().decode("utf-8", errors="replace")
            # Skip if it bounced to login
            if "<title>Login</title>" in html or "Login.aspx" in r.url:
                continue
            # Heuristic: we want a page that mentions multiple companies / "holder" / "bidder"
            l = html.lower()
            if any(kw in l for kw in ("plan holder", "planholder", "bidder list",
                                       "bidders list", "registered companies",
                                       "registered bidder", "general contractor")):
                return url, html
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
    return None, ""


# Crude HTML helpers (no BeautifulSoup -- keep deps minimal)

def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "")


def collapse_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def parse_detail(html):
    """Best-effort extraction of structured fields from filter.aspx HTML.
    Returns dict; missing fields are None. Discovery-mode-tolerant: if a
    field doesn't match expected patterns, it stays None and we keep
    the raw HTML so we can refine later."""
    out = {
        "long_description": None,
        "owner_contact_name": None,
        "owner_contact_phone": None,
        "owner_contact_email": None,
        "owner_address": None,
        "bid_date": None,
        "pre_bid_meeting": None,
        "pre_bid_meeting_at": None,
        "pre_bid_meeting_location": None,
        "pre_bid_meeting_mandatory": None,
        "bid_security": None,
        "estimated_value": None,
        "project_duration": None,
        "addenda_count": 0,
        "addenda_list": [],
        "doc_count": 0,
        "doc_list": [],
        "plan_holders": [],   # extracted from GC table on main detail page
        "warnings": [],
    }

    # Extract plan holders / GC table from main detail page.
    # SBX embeds the bidder/plan-holder table directly on filter.aspx.
    # Table header = "General Contractor" (or "Plan Holders"); rows have:
    #   company name (may end with * = confirmed), email, Ph:xxx, City ST
    gc_table_rx = re.compile(
        r"(?:General\s+Contractor|Plan\s+Holders?|Bidder[s]?\s+List)"
        r".*?</tr>(.*?)(?:</table>|<table)",
        re.IGNORECASE | re.DOTALL,
    )
    gc_match = gc_table_rx.search(html)
    if gc_match:
        tr_pat2 = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
        td_pat2 = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
        for tr in tr_pat2.findall(gc_match.group(1)):
            cells = [collapse_ws(unescape(strip_tags(c))) for c in td_pat2.findall(tr)]
            if not cells: continue
            name = cells[0].rstrip(" *").strip()
            if not name or len(name) < 3 or len(name) > 150: continue
            if not re.search(r"[A-Za-z]", name): continue
            confirmed = cells[0].strip().endswith("*")
            email = next((c for c in cells if "@" in c and "." in c), None)
            phone_raw = next((c for c in cells if re.search(r"\d{3}[\s.(/-]?\d{3}", c)), None)
            phone = re.sub(r"^Ph:\s*", "", phone_raw or "").strip() if phone_raw else None
            city_cell = cells[-1] if len(cells) >= 3 else None
            out["plan_holders"].append({
                "name": name,
                "confirmed": confirmed,
                "email": email,
                "phone": phone,
                "city": city_cell,
            })

    text = collapse_ws(strip_tags(html))

    # Email + phone heuristics (work even without knowing exact tag structure)
    em = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if em: out["owner_contact_email"] = em.group(0)
    ph = re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text)
    if ph: out["owner_contact_phone"] = ph.group(0)

    # Pre-bid meeting block: usually labeled in caps. We capture the raw
    # block AND parse structured fields (date, time, location, mandatory)
    # because the existing job-walk-invite cron + BID BREAKDOWN autofill
    # both want these as separate fields.
    m = re.search(r"(pre[- ]?bid[^.]{0,300})", text, re.IGNORECASE)
    if m:
        block = collapse_ws(m.group(1))[:500]
        out["pre_bid_meeting"] = block

        # Date/time: SBX commonly shows "5/14 @ 10AM" or "5/14/2026 at 10:00 AM"
        # Allow the year to be missing -- assume CURRENT year unless that
        # date already passed, then bump to next year.
        dt_match = re.search(
            r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?"      # 5/14 or 5/14/2026
            r"\s*(?:@|at)?\s*"
            r"(\d{1,2})(?::(\d{2}))?\s*([apAP][mM])?",
            block,
        )
        if dt_match:
            try:
                mo = int(dt_match.group(1)); dy = int(dt_match.group(2))
                yr_raw = dt_match.group(3)
                if yr_raw:
                    yr = int(yr_raw)
                    if yr < 100: yr += 2000
                else:
                    today = datetime.now()
                    yr = today.year
                    candidate = datetime(yr, mo, dy)
                    if candidate.date() < today.date():
                        yr += 1
                hh = int(dt_match.group(4)); mn = int(dt_match.group(5) or 0)
                ampm = (dt_match.group(6) or "").lower()
                if ampm == "pm" and hh < 12: hh += 12
                if ampm == "am" and hh == 12: hh = 0
                # Treat as Pacific local; the cron sends invites in PT
                out["pre_bid_meeting_at"] = datetime(yr, mo, dy, hh, mn).isoformat()
            except Exception as e:
                out["warnings"].append(f"pre_bid_meeting_at parse failed: {e}")

        # Location: usually in parens after the date
        loc_match = re.search(r"\(([^)]+)\)", block)
        if loc_match:
            out["pre_bid_meeting_location"] = collapse_ws(loc_match.group(1))[:240]

        # Mandatory: look for explicit "Mandatory: Yes/No"
        mand_match = re.search(r"mandatory\s*:?\s*(yes|no)", block, re.IGNORECASE)
        if mand_match:
            out["pre_bid_meeting_mandatory"] = mand_match.group(1).lower() == "yes"

    # Bid date+time -- look for "Bid Date:" or "Bids Due:" patterns
    m = re.search(r"(?:bid date|bids? due|due date)\s*:?\s*([\d/\-]{8,10}(?:\s+\d{1,2}:\d{2}\s*[apAP][mM])?)", text, re.IGNORECASE)
    if m:
        try:
            v = m.group(1).strip()
            for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
                try:
                    dt = datetime.strptime(v, fmt)
                    out["bid_date"] = dt.replace(tzinfo=timezone.utc).isoformat()
                    break
                except ValueError:
                    continue
        except Exception:
            out["warnings"].append(f"bid_date parse failed: {m.group(1)!r}")

    # Estimated value
    m = re.search(r"(?:estimated value|estimate value|engineer.s estimate|construction cost estimate)\s*:?\s*([\$\d,.\s\-Mk]+)", text, re.IGNORECASE)
    if m:
        out["estimated_value"] = collapse_ws(m.group(1))[:80]

    # Project duration
    m = re.search(r"(?:duration|construction days|completion)[^:]*:\s*([\w\d\s,.\-]+)(?=\.|$)", text, re.IGNORECASE)
    if m:
        out["project_duration"] = collapse_ws(m.group(1))[:120]

    # Bid security / bond
    m = re.search(r"(bid (?:bond|security)[^.]{0,200})", text, re.IGNORECASE)
    if m:
        out["bid_security"] = collapse_ws(m.group(1))[:200]

    # Long description: look for div/td with class hinting at description.
    for pat in [
        r'<(?:div|td|span)[^>]*class="[^"]*(?:description|projdesc|projectdesc|desc)[^"]*"[^>]*>(.*?)</(?:div|td|span)>',
        r'<(?:div|td|span)[^>]*id="[^"]*(?:description|Desc)[^"]*"[^>]*>(.*?)</(?:div|td|span)>',
    ]:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            out["long_description"] = collapse_ws(unescape(strip_tags(m.group(1))))[:2000]
            break

    # Addenda: look for table rows or list items mentioning "Addendum N"
    addenda = []
    for m in re.finditer(r"addend(?:um|a)\s*#?\s*(\d+)\s*(?:[-:]|<)?\s*([^<\n]{0,80})", text, re.IGNORECASE):
        n = int(m.group(1))
        if not any(a["number"] == n for a in addenda):
            addenda.append({"number": n, "raw": collapse_ws(m.group(0))[:120]})
    out["addenda_count"] = len(addenda)
    out["addenda_list"] = addenda

    return out


def parse_planholders(html):
    """Extract plan holder rows from the planholder list page. Tries a few
    HTML structures (table, list, repeated divs). Returns list of dicts:
        [{name, contact, phone, email, city, role, doc_count}]
    Discovery-mode-tolerant -- if no structure matches, returns empty list
    and the raw HTML is preserved on the row for later inspection."""
    holders = []
    # Pattern A: <tr> rows with company in first cell, contact in second, etc.
    tr_pat = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    td_pat = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
    for tr in tr_pat.findall(html):
        cells = [collapse_ws(unescape(strip_tags(c))) for c in td_pat.findall(tr)]
        if not cells or len(cells) < 1:
            continue
        # Skip header rows: ones that contain words like "Company Name" / "Contact"
        if any(c.lower() in ("company", "company name", "contact", "phone", "email", "city") for c in cells):
            continue
        name = cells[0]
        if not name or len(name) < 3 or len(name) > 120:
            continue
        # Filter out obvious non-company rows (pagination, navigation, summaries)
        if name.lower() in ("plan holders", "bidders", "next", "prev", "back"):
            continue
        # Heuristic: company-like names have at least one letter and no leading numeric
        if not re.search(r"[A-Za-z]", name):
            continue
        holder = {
            "name": name,
            "contact_name": cells[1] if len(cells) > 1 else None,
            "contact_phone": next((c for c in cells if re.search(r"\d{3}[\s.-]?\d{3}[\s.-]?\d{4}", c)), None),
            "contact_email": next((c for c in cells if "@" in c and "." in c), None),
            "city": None,
            "role": None,
            "doc_count": 0,
        }
        holders.append(holder)
    # Dedup by normalized name
    seen = {}
    for h in holders:
        k = normalize_gc(h["name"])
        if k and k not in seen:
            seen[k] = h
    return list(seen.values())


# --- Main --------------------------------------------------------------------

def main():
    limit = int(os.environ.get("SBX_DETAIL_LIMIT") or DEFAULT_LIMIT)
    discovery = (os.environ.get("SBX_DETAIL_DISCOVERY") or "1").strip() == "1"
    min_interval = int(os.environ.get("SBX_DETAIL_MIN_INTERVAL_MIN") or DEFAULT_MIN_INTERVAL_MIN)

    print(f"SBX details scrape: limit={limit} discovery={discovery} min_interval_min={min_interval}")
    started = time.time()

    # Fetch ALL future SBX listings (not just ones Alex set up) so we
    # monitor every active project for addenda + bid date + bidder changes.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    st_l, body_l = _sb("GET",
        f"sbx_listings_cloud?select=opsplannum,bid_package_id,project_name,bid_date"
        f"&bid_date=gte.{today_iso}&order=bid_date.asc&limit={limit * 3}")
    all_listings_raw = json.loads(body_l) if st_l == 200 else []
    seen_pn = {}
    for r in all_listings_raw:
        seen_pn.setdefault(r["opsplannum"], r)
    listings = list(seen_pn.values())
    print(f"  candidate active SBX listings (future bid date): {len(listings)}")
    if not listings:
        print("  no active SBX listings to scrape, exiting.")
        return

    recent = fetch_recent_detail_fetches()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_interval)
    todo = []
    for r in listings:
        last_iso = recent.get(r["opsplannum"])
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
                if last_dt > cutoff:
                    continue
            except Exception:
                pass
        todo.append(r)
        if len(todo) >= limit:
            break
    print(f"  to fetch this run: {len(todo)} (skipped {len(listings) - len(todo)} recently-fetched)")
    if not todo:
        print("  nothing due, exiting.")
        return

    opener = sbx_login()

    detail_rows = []
    holder_rows = []

    for i, r in enumerate(todo, 1):
        plannum = r["opsplannum"]
        bpid = r["bid_package_id"]
        if not bpid:
            print(f"  [{i}/{len(todo)}] {plannum}: no bid_package_id, skipping")
            continue

        print(f"  [{i}/{len(todo)}] {plannum} (BPID={bpid}) ...", end=" ", flush=True)
        st, html, url = fetch_detail(opener, bpid, plannum)
        if st != 200 or not html:
            print(f"detail fetch HTTP {st}")
            continue
        if "<title>Login</title>" in html:
            print("bounced to login -- session expired? aborting run")
            break

        parsed = parse_detail(html)

        ph_url, ph_html = try_planholder_endpoints(opener, bpid, plannum)
        holders = parse_planholders(ph_html) if ph_html else []
        print(f"detail OK ({len(html)//1024}KB), planholder src: {ph_url or 'none'} ({len(holders)} parsed)")
        # DISCOVERY: dump distinctive snippets of planholder HTML on first project
        # only, so we can see the structure without flooding the log.
        if discovery and i == 1 and ph_html:
            print(f"\n--- PLANHOLDER HTML SAMPLE (first {min(4000, len(ph_html))} chars) ---")
            print(ph_html[:4000])
            print("--- END SAMPLE ---\n")
            # Also print any class/id attributes that might be anchor points
            classes = sorted(set(re.findall(r'class="([^"]+)"', ph_html)))[:30]
            ids = sorted(set(re.findall(r'id="([^"]+)"', ph_html)))[:30]
            print(f"  ph classes (sample): {classes}")
            print(f"  ph ids (sample): {ids}")
            # Count tables/rows
            print(f"  ph <table> count: {ph_html.lower().count('<table')}")
            print(f"  ph <tr> count: {ph_html.lower().count('<tr')}")
            print(f"  ph <li> count: {ph_html.lower().count('<li')}\n")

        now_iso = datetime.now(timezone.utc).isoformat()
        detail_row = {
            "opsplannum":             plannum,
            "bid_package_id":         bpid,
            "project_name":           r.get("project_name"),
            "long_description":       parsed.get("long_description"),
            "owner_contact_name":     parsed.get("owner_contact_name"),
            "owner_contact_phone":    parsed.get("owner_contact_phone"),
            "owner_contact_email":    parsed.get("owner_contact_email"),
            "owner_address":          parsed.get("owner_address"),
            "bid_date":               parsed.get("bid_date"),
            "pre_bid_meeting":           parsed.get("pre_bid_meeting"),
            "pre_bid_meeting_at":        parsed.get("pre_bid_meeting_at"),
            "pre_bid_meeting_location":  parsed.get("pre_bid_meeting_location"),
            "pre_bid_meeting_mandatory": parsed.get("pre_bid_meeting_mandatory"),
            "bid_security":           parsed.get("bid_security"),
            "estimated_value":        parsed.get("estimated_value"),
            "project_duration":       parsed.get("project_duration"),
            "addenda_count":          parsed.get("addenda_count") or 0,
            "addenda_list":           parsed.get("addenda_list") or [],
            "bidder_count":           len(holders) or len(parsed.get("plan_holders") or []),
            "raw_html":               html if discovery else None,
            "raw_planholder_html":    ph_html if discovery else None,
            "parse_warnings":         parsed.get("warnings") or [],
            "fetched_at":             now_iso,
            "updated_at":             now_iso,
        }
        detail_rows.append(detail_row)

        # Merge plan holders from detail page + separate planholder page
        all_holders = list(holders)
        detail_phs = parsed.get("plan_holders") or []
        seen_ph_names = {normalize_gc(h["name"]) for h in all_holders}
        for ph in detail_phs:
            nm = normalize_gc(ph["name"])
            if nm and nm not in seen_ph_names:
                all_holders.append({
                    "name": ph["name"],
                    "contact_phone": ph.get("phone"),
                    "contact_email": ph.get("email"),
                    "city": ph.get("city"),
                    "role": "GC",
                    "doc_count": 0,
                })
                seen_ph_names.add(nm)
        if detail_phs:
            print(f"  +{len(detail_phs)} GCs from detail page ({len(all_holders)} total)")

        for h in all_holders:
            nm_norm = normalize_gc(h["name"])
            if not nm_norm:
                continue
            holder_rows.append({
                "id":                  f"{plannum}::{nm_norm}",
                "opsplannum":          plannum,
                "gc_name":             h["name"],
                "gc_name_normalized":  nm_norm,
                "contact_name":        h.get("contact_name"),
                "contact_phone":       h.get("contact_phone"),
                "contact_email":       h.get("contact_email"),
                "city":                h.get("city"),
                "role":                h.get("role"),
                "doc_count":           h.get("doc_count") or 0,
                "status":              "active",
                "last_seen_at":        now_iso,
                "updated_at":          now_iso,
            })

        time.sleep(0.4)

    print(f"\nUpserting {len(detail_rows)} project details, {len(holder_rows)} plan holders ...")
    n_d = upsert("sbx_project_details_cloud", detail_rows)
    n_h = upsert("sbx_plan_holders_cloud", holder_rows)
    print(f"  details: {n_d}  holders: {n_h}")

    # Mark plan holders not seen this run as "dropped" only if they belong to a
    # project we DID rescan -- we don't want to mark dropped on projects we
    # didn't fetch this run.
    fetched_plannums = list({d["opsplannum"] for d in detail_rows})
    if fetched_plannums:
        # PostgREST in.() needs URL-quoted commas
        in_clause = ",".join(urllib.parse.quote(p, safe="") for p in fetched_plannums)
        # Mark stale rows as 'dropped' for these projects
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        # We just upserted with last_seen_at=now -- anything still older is gone from the page
        st, _ = _sb("PATCH",
                    f"sbx_plan_holders_cloud?opsplannum=in.({in_clause})&last_seen_at=lt.{urllib.parse.quote(cutoff_iso)}&status=eq.active",
                    body={"status": "dropped", "updated_at": datetime.now(timezone.utc).isoformat()})
        if st in (200, 204):
            print("  marked stale holders as dropped")

    print(f"Done in {round(time.time() - started, 1)}s")


if __name__ == "__main__":
    main()
