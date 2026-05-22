"""send-weekly-digest.py -- Sunday-evening company-wide roll-up email.

What the week looked like across:
  - Bids: how many entered BIDDING, SENT, AWARDED, NOT AWARDED this week
  - Addenda: total new addenda detected, broken down by source
  - Portal updates: commit volume across the two repos
  - Watcher: total bid setups completed
  - Active bids count

Schedule: Sundays 6:00 PM PT (= Mon 01:00 UTC in PDT, 02:00 UTC in PST).

Recipients pulled from email_policies_cloud.weekly_digest.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

from _email_policies import get_recipients_for, record_send  # type: ignore


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
REPOS = [
    ("Portal", "alex-fusionelectric/fusion-bid-list"),
    ("Cron",   "alex-fusionelectric/fusion-cloud-cron"),
]


# --- Supabase helper ---------------------------------------------------

def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required")
    return k


def _sb(path: str) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {"apikey": _service_key(), "Authorization": f"Bearer {_service_key()}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")) or []
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[warn] Supabase GET failed {path}: {e}", file=sys.stderr)
        return []


# --- Week boundaries (Pacific) -----------------------------------------

def week_window():
    """Returns (start_utc_iso, end_utc_iso, label) for the Mon-Sun week
    that just ended. We treat 'this week' as Mon 00:00 PT through Sun
    23:59 PT, so the Sunday-evening cron always reports a complete week."""
    now = datetime.now(timezone.utc)
    # Find the Monday of the week containing 'now', in UTC.
    # Pacific is UTC-7 (PDT) -- the cron schedule already commits to that.
    pt_now = now - timedelta(hours=7)
    monday = pt_now.date() - timedelta(days=pt_now.weekday())
    start_pt = datetime(monday.year, monday.month, monday.day, 0, 0, 0,
                        tzinfo=timezone(timedelta(hours=-7)))
    end_pt = start_pt + timedelta(days=7)
    start_utc = start_pt.astimezone(timezone.utc)
    end_utc = end_pt.astimezone(timezone.utc)
    label = f"{start_pt.strftime('%b %-d')} – {(end_pt - timedelta(days=1)).strftime('%b %-d, %Y')}"
    return start_utc.isoformat(), end_utc.isoformat(), label


# --- Data gathers -------------------------------------------------------

def gather_bids_summary(start_iso: str, end_iso: str) -> dict:
    """Counts of bids by status/outcome within the week, plus current
    active count. Reads bids_cloud (the BID LIST mirror)."""
    # Active right now
    active_rows = _sb("bids_cloud?select=est_number,status,outcome,updated_at"
                     "&status=in.(BIDDING,%22BID%20OR%20BAIL%22,SENT,FOLLOW%20UP,FOLLOW%20UPS,PENDING)"
                     "&limit=600")
    # Anything that changed status/outcome within the week
    week_rows = _sb(f"bids_cloud?select=est_number,status,outcome,updated_at"
                    f"&updated_at=gte.{urllib.parse.quote(start_iso)}"
                    f"&updated_at=lt.{urllib.parse.quote(end_iso)}"
                    f"&limit=1000")
    def count_with(outcome_pat=None, status_pat=None):
        c = 0
        for r in week_rows:
            o = (r.get("outcome") or "").lower()
            s = (r.get("status") or "").upper()
            if outcome_pat and outcome_pat in o: c += 1
            if status_pat and status_pat == s:   c += 1
        return c
    return {
        "active_now": len(active_rows),
        "week_bidding_new":     count_with(status_pat="BIDDING"),
        "week_sent":            count_with(status_pat="SENT"),
        "week_awarded":         count_with(outcome_pat="awarded") - count_with(outcome_pat="not awarded"),
        "week_not_awarded":     count_with(outcome_pat="not awarded"),
    }


def gather_addenda_summary(start_iso: str, end_iso: str) -> dict:
    rows = _sb(f"bid_addenda_cloud?select=est_number,found_in_gmail,found_in_folder,detected_at"
               f"&detected_at=gte.{urllib.parse.quote(start_iso)}"
               f"&detected_at=lt.{urllib.parse.quote(end_iso)}"
               f"&limit=2000")
    total = len(rows)
    gmail = sum(1 for r in rows if r.get("found_in_gmail"))
    folder = sum(1 for r in rows if r.get("found_in_folder"))
    bids_touched = len({r.get("est_number") for r in rows if r.get("est_number")})
    return {"total": total, "from_gmail": gmail, "from_folder": folder,
            "bids_touched": bids_touched}


def gather_completions_summary(start_iso: str, end_iso: str) -> int:
    rows = _sb(f"bid_setup_completions_cloud?select=est_number,completed_at"
               f"&completed_at=gte.{urllib.parse.quote(start_iso)}"
               f"&completed_at=lt.{urllib.parse.quote(end_iso)}"
               f"&limit=500")
    return len(rows)


def gather_commits_summary(start_iso: str, end_iso: str) -> dict:
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "fusion-weekly-digest"}
    if token: headers["Authorization"] = f"Bearer {token}"
    total = 0
    per_repo = []
    for label, repo in REPOS:
        url = (f"https://api.github.com/repos/{repo}/commits"
               f"?since={urllib.parse.quote(start_iso)}&until={urllib.parse.quote(end_iso)}"
               f"&per_page=100")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                rows = json.loads(r.read().decode("utf-8")) or []
                per_repo.append({"label": label, "repo": repo, "count": len(rows)})
                total += len(rows)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"[warn] GitHub fetch failed {repo}: {e}", file=sys.stderr)
            per_repo.append({"label": label, "repo": repo, "count": 0})
    return {"total": total, "per_repo": per_repo}


# --- Render -------------------------------------------------------------

def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(label: str, bids: dict, addenda: dict, completions: int, commits: dict) -> tuple[str, str]:
    plain = [f"Fusion Electric -- weekly digest",
             f"Week of {label}", ""]
    plain += [f"Bids: {bids['week_bidding_new']} new BIDDING · {bids['week_sent']} SENT · "
              f"{bids['week_awarded']} AWARDED · {bids['week_not_awarded']} NOT AWARDED",
              f"Active right now: {bids['active_now']}",
              "",
              f"Setups completed: {completions}",
              "",
              f"Addenda detected: {addenda['total']} across {addenda['bids_touched']} bids "
              f"({addenda['from_gmail']} via Gmail, {addenda['from_folder']} via folder)",
              "",
              f"Portal updates: {commits['total']} commits"]
    for r in commits["per_repo"]:
        plain.append(f"  - {r['label']:6} {r['count']:>3}  ({r['repo']})")

    def stat_card(label, value, sub="", color="#7dd3fc"):
        return (f'<div style="flex:1;min-width:140px;background:#f8fafc;border:1px solid #e2e8f0;'
                f'border-radius:8px;padding:12px 14px;">'
                f'<div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;font-weight:800;color:{color};">{esc(label)}</div>'
                f'<div style="font-size:24px;font-weight:900;color:#0f172a;margin-top:4px;">{esc(str(value))}</div>'
                f'<div style="font-size:11px;color:#64748b;margin-top:2px;">{esc(sub)}</div>'
                f'</div>')

    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
<div style="max-width:680px;margin:24px auto;">
  <div style="background:#0f172a;padding:24px 28px;border-radius:10px 10px 0 0;">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#475569;margin-bottom:6px;">Fusion Electric &middot; Weekly Digest</div>
    <div style="font-size:24px;font-weight:900;color:#f8fafc;">Week of {esc(label)}</div>
  </div>
  <div style="background:#fff;color:#0f172a;padding:22px 28px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 10px 10px;">
    <div style="font-size:11px;font-weight:900;color:#34d399;text-transform:uppercase;letter-spacing:.6px;margin-bottom:10px;">Bids</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;">
      {stat_card("Active now", bids["active_now"], "Across BIDDING / SENT / etc.", "#34d399")}
      {stat_card("New BIDDING", bids["week_bidding_new"], "this week", "#7dd3fc")}
      {stat_card("Sent", bids["week_sent"], "this week", "#7dd3fc")}
      {stat_card("Awarded", bids["week_awarded"], "this week", "#34d399")}
      {stat_card("Not awarded", bids["week_not_awarded"], "this week", "#f87171")}
    </div>

    <div style="font-size:11px;font-weight:900;color:#fbbf24;text-transform:uppercase;letter-spacing:.6px;margin-bottom:10px;">Pipeline</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;">
      {stat_card("Setups completed", completions, "watcher pipeline runs", "#fbbf24")}
      {stat_card("Addenda detected", addenda["total"], f"across {addenda['bids_touched']} bid(s)", "#fbbf24")}
      {stat_card("From Gmail", addenda["from_gmail"], "addenda sources", "#fbbf24")}
      {stat_card("From folder", addenda["from_folder"], "addenda sources", "#fbbf24")}
    </div>

    <div style="font-size:11px;font-weight:900;color:#a78bfa;text-transform:uppercase;letter-spacing:.6px;margin-bottom:10px;">Software updates</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;">
      {stat_card("Commits", commits["total"], "total this week", "#a78bfa")}
      {''.join(stat_card(r["label"], r["count"], r["repo"], "#a78bfa") for r in commits["per_repo"])}
    </div>

    <div style="border-top:1px solid #e2e8f0;padding-top:12px;margin-top:8px;font-size:11px;color:#64748b;">
      Per email_policies_cloud.weekly_digest &middot;
      manage recipients at <a href="https://fusion-main-panel.netlify.app/admin/#email">admin/Email Center</a>.
    </div>
  </div>
</div>
</body></html>"""
    return "\n".join(plain), html


# --- Gmail send ---------------------------------------------------------

def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required")
    creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(svc, sender, recipients, subject, plain, html):
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


# --- Main ---------------------------------------------------------------

def main():
    print(f"=== send-weekly-digest started at {datetime.now(timezone.utc).isoformat()} ===")
    recipients = get_recipients_for("weekly_digest")
    if not recipients:
        print("  [policy] weekly_digest disabled OR has no recipients -- skipping.")
        return
    print(f"  [policy] recipients = {recipients}")

    start_iso, end_iso, label = week_window()
    print(f"  week window: {start_iso} -> {end_iso} ({label})")

    bids        = gather_bids_summary(start_iso, end_iso)
    addenda     = gather_addenda_summary(start_iso, end_iso)
    completions = gather_completions_summary(start_iso, end_iso)
    commits     = gather_commits_summary(start_iso, end_iso)

    plain, html = render(label, bids, addenda, completions, commits)
    subject = f"Fusion Electric -- week of {label}"

    sender = (os.environ.get("GMAIL_FROM") or "alex@fusionelectric-inc.com").strip()
    try:
        svc = gmail_service()
        send_email(svc, sender, recipients, subject, plain, html)
        record_send("weekly_digest", recipients)
        print(f"  [sent] weekly digest -> {recipients}")
    except Exception as e:  # noqa: BLE001
        print(f"[err] gmail send failed: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
