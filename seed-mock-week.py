"""seed-mock-week.py — Populate the field panel with one realistic
week of time entries across 3 mock field guys, 6 Bay Area jobsites,
and a mix of approval states (approved / foreman-only / pending).

Re-running is idempotent: deletes any prior rows for the mock users
before re-inserting.

Usage:
    python seed-mock-week.py                # seeds the most recent Mon–Fri
    python seed-mock-week.py --wipe         # delete the mock users + entries and exit
"""
import argparse
import datetime as dt
import hashlib
import os
import random
import sys
import urllib.request
import urllib.error
import json

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://dltuvsdwrujjsmiotaxy.supabase.co")
SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if not SERVICE_KEY:
    sys.exit("Missing SUPABASE_SERVICE_KEY env var.")

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    # merge-duplicates makes ?on_conflict=… actually upsert instead of erroring
    "Prefer": "return=representation,resolution=merge-duplicates",
}

# Mock field crew. Foreman_id = u-alex so they show under Alex's approval queue.
MOCK_USERS = [
    {"id": "u-mock-carlos",  "username": "carlos.mock",  "display_name": "Carlos Reyes"},
    {"id": "u-mock-diego",   "username": "diego.mock",   "display_name": "Diego Martinez"},
    {"id": "u-mock-marcus",  "username": "marcus.mock",  "display_name": "Marcus Williams"},
]

# Real active BAY jobs from budgets_cloud — uses the live full_number
# format (####-BAY) so the time entries look like the real thing,
# not est numbers (##-###). Coordinates approximate the actual sites.
PROJECTS = [
    {"est": "2541-BAY", "name": "LPCH SSS BUILD OUT",         "lat": 37.433900, "lng": -122.176100},
    {"est": "2611-BAY", "name": "FUSD CAB, GLEN, PATT HVAC",  "lat": 37.548300, "lng": -121.988600},
    {"est": "2518-BAY", "name": "OAKLAND WAREHOUSE",          "lat": 37.804400, "lng": -122.271200},
    {"est": "2535-BAY", "name": "BERRYESSA WAREHOUSE",        "lat": 37.383800, "lng": -121.855200},
    {"est": "2537-BAY", "name": "OCH ICU OUTLETS",            "lat": 37.452765, "lng": -122.145665},
    {"est": "2603-BAY", "name": "SHC MRI TRAILER",            "lat": 37.435600, "lng": -122.174500},
]

# Comments rotated across entries — mix of work notes, blockers, and
# offhand jobsite chatter so the entry-detail modal looks lived-in.
NOTES_POOL = [
    "Pulled new feeder. Ran 3# 350MCM in EMT to MSB.",
    "Trimmed out break room. Tested all receps, all good.",
    "Switchgear delivered ~10am, helped offload from lift gate.",
    "Scissor lift down ~30 min waiting on rental swap.",
    "Wrapped up panel schedule labels in main electrical room.",
    "Inspector signed off underground rough.",
    "GC asked about adding 2 floor boxes in conf room — emailed PM.",
    "Worked with low voltage sub on data drops in ceiling grid.",
    "Pulled wire on east wing classrooms. Good pace.",
    "Coordinated with mechanical on chiller power feed.",
    "Helped foreman lay out panel locations on 2nd floor.",
    "Cleaned up scraps and rolled cords end of day.",
    "Bent and stubbed up emt for site lighting circuits.",
    "Terminated panel A in main switch room — 42-circuit.",
    "Owner walked the site, pointed out a few punch items.",
    None, None,  # some entries leave notes blank
]

# ── helpers ─────────────────────────────────────────────────────────
def http(method, path, body=None):
    url = f"{SUPABASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        sys.exit(f"HTTP {e.code} on {method} {path}: {body}")

def hash_password(password, salt):
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()

def jitter(coord, m=0.0006):
    """Tiny GPS wobble (~50m) so pins aren't stacked exactly."""
    return round(coord + random.uniform(-m, m), 6)

def iso(d):
    """Local Pacific datetime → UTC ISO string. Mock clocks are PT,
    server stores UTC; +8h is close enough for May (PDT = UTC-7, but
    one-hour drift on a mock dataset is harmless)."""
    return (d + dt.timedelta(hours=8)).isoformat() + "Z"

def date_str(d):
    return d.strftime("%Y-%m-%d")

# ── core seeding logic ─────────────────────────────────────────────
def upsert_mock_users():
    salt = "mockmockmockmock"
    pw_hash = hash_password("changeme", salt)
    rows = [{
        "id": u["id"],
        "company_id": "fusion-electric",
        "username": u["username"],
        "display_name": u["display_name"],
        "role": "foreman",
        "password_salt": salt,
        "password_hash": pw_hash,
        "foreman_id": "u-alex",
        "active": True,
        "tracking_acknowledged_at": "2026-05-04T15:00:00Z",
        "tracking_policy_version": "1.0-2026-05-08",
        "gps_consent": True,
    } for u in MOCK_USERS]
    # Upsert via on_conflict on primary key
    http("POST", "/rest/v1/app_users?on_conflict=id", rows)
    print(f"  -{len(rows)} mock users upserted")

def wipe_mock():
    ids = ",".join(f'"{u["id"]}"' for u in MOCK_USERS)
    # Delete entries first (FK), then users
    http("DELETE", f"/rest/v1/field_time_entries_cloud?user_id=in.({ids})")
    http("DELETE", f"/rest/v1/app_users?id=in.({ids})")
    print(f"  -wiped mock users + entries")

def build_entries(week_start):
    """Generate one week of entries. week_start is a Monday date."""
    rows = []

    # Per-user templates: each user gets a different daily pattern so
    # the approval queue doesn't look like a stamped-out grid.
    # Every guy bounces between sites most days so the day-expansion
    # in the approval queue actually has multiple rows to show. Job
    # codes match real budgets_cloud full_numbers.
    schedules = {
        "u-mock-carlos": [
            # Mon — split (LPCH trim → SHC punch list)
            [("2541-BAY", "06:30", "11:00", 0), ("2603-BAY", "12:00", "15:30", 0)],
            # Tue — split (LPCH → OCH ICU)
            [("2541-BAY", "06:30", "11:30", 0), ("2537-BAY", "12:15", "15:30", 0)],
            # Wed — single (focused day at OCH ICU outlets)
            [("2537-BAY", "06:45", "15:15", 30)],
            # Thu — three-stop coordination day
            [("2541-BAY", "06:30", "10:00", 0),
             ("2603-BAY", "10:30", "13:00", 0),
             ("2537-BAY", "13:45", "16:30", 0)],
            # Fri — split, short Friday
            [("2541-BAY", "06:30", "10:30", 0), ("2603-BAY", "11:00", "13:30", 0)],
        ],
        "u-mock-diego": [
            # Mon — single FUSD HVAC
            [("2611-BAY", "07:00", "15:30", 30)],
            # Tue — split (FUSD → Oakland Warehouse for vendor meeting)
            [("2611-BAY", "07:00", "11:00", 0), ("2518-BAY", "12:00", "15:30", 0)],
            # Wed — three stops (warehouse pickup midday)
            [("2611-BAY", "07:00", "11:00", 0),
             ("2518-BAY", "11:45", "13:30", 0),
             ("2611-BAY", "14:00", "16:00", 0)],
            # Thu — split (Oakland morning, back to FUSD)
            [("2518-BAY", "07:00", "11:00", 0), ("2611-BAY", "12:00", "15:30", 0)],
            # Fri — single FUSD half day
            [("2611-BAY", "07:00", "12:30", 0)],
        ],
        "u-mock-marcus": [
            # Mon — split (Berryessa → Oakland Warehouse)
            [("2535-BAY", "06:30", "11:30", 0), ("2518-BAY", "12:30", "16:30", 0)],
            # Tue — three stops (Berryessa → Oakland → back to Berryessa)
            [("2535-BAY", "06:30", "10:00", 0),
             ("2518-BAY", "10:45", "13:00", 0),
             ("2535-BAY", "13:45", "16:30", 0)],
            # Wed — split (Berryessa → OCH ICU)
            [("2535-BAY", "06:30", "11:30", 0), ("2537-BAY", "12:30", "15:30", 0)],
            # Thu — single Oakland Warehouse
            [("2518-BAY", "07:00", "15:30", 30)],
            # Fri — split (Berryessa → Oakland)
            [("2535-BAY", "06:30", "11:00", 0), ("2518-BAY", "12:00", "15:30", 0)],
        ],
    }
    proj_by_key = {p["est"]: p for p in PROJECTS}
    user_by_id = {u["id"]: u for u in MOCK_USERS}

    for user_id, week in schedules.items():
        u = user_by_id[user_id]
        for day_idx, day in enumerate(week):
            day_date = week_start + dt.timedelta(days=day_idx)
            for stop_idx, (pkey, t_in, t_out, lunch) in enumerate(day):
                proj = proj_by_key[pkey]
                hh_in,  mm_in  = map(int, t_in.split(":"))
                hh_out, mm_out = map(int, t_out.split(":"))
                in_dt  = dt.datetime.combine(day_date, dt.time(hh_in,  mm_in))
                out_dt = dt.datetime.combine(day_date, dt.time(hh_out, mm_out))

                # Approval spread: Mon-Wed approved, Thu-Fri pending. (When
                # the two-tier migration ships, swap a couple of rows in to
                # show the foreman-approved-only state too.)
                approved_at = (out_dt + dt.timedelta(hours=14)) if day_idx <= 2 else None

                rows.append({
                    "id": f"fte-mock-{user_id[-6:]}-{day_date}-{stop_idx}",
                    "user_id": user_id,
                    "username": u["username"],
                    "display_name": u["display_name"],
                    "date": date_str(day_date),
                    "project_est": proj["est"],
                    "project_name": proj["name"],
                    "time_in":  iso(in_dt),
                    "time_out": iso(out_dt),
                    "lunch_minutes": lunch,
                    "notes": random.choice(NOTES_POOL),
                    "gps_in_lat":  jitter(proj["lat"]),
                    "gps_in_lng":  jitter(proj["lng"]),
                    "gps_out_lat": jitter(proj["lat"]),
                    "gps_out_lng": jitter(proj["lng"]),
                    "approved_at": iso(approved_at) if approved_at else None,
                    "approved_by": "u-alex"          if approved_at else None,
                })
    return rows

def insert_entries(rows):
    # Insert in batches; PostgREST handles arrays natively.
    http("POST", "/rest/v1/field_time_entries_cloud?on_conflict=id", rows)
    print(f"  -{len(rows)} entries inserted")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wipe", action="store_true", help="Delete mock users + entries and exit")
    args = p.parse_args()

    if args.wipe:
        wipe_mock()
        return

    random.seed(42)  # deterministic notes

    # Find most recent Monday on or before today.
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    print(f"Seeding week starting {week_start} (Mon)…")

    upsert_mock_users()

    # Drop any stale entries for these users so re-runs are clean.
    ids = ",".join(f'"{u["id"]}"' for u in MOCK_USERS)
    http("DELETE", f"/rest/v1/field_time_entries_cloud?user_id=in.({ids})")

    rows = build_entries(week_start)
    insert_entries(rows)

    print("\nMock week seeded. Open the field panel admin tab to view.")
    print("Run with --wipe to remove.")

if __name__ == "__main__":
    main()
