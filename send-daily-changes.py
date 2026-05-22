"""send-daily-changes.py -- "What shipped today" digest email.

Pulls every commit landed in the last 24h across
  alex-fusionelectric/fusion-bid-list      (Portal)
  alex-fusionelectric/fusion-cloud-cron    (Cron)
groups them by area (parsed from the commit subject prefix), and emails
an HTML digest to email_policies_cloud.daily_changes recipients.

Schedule: 5:30 PM PT (M-F) via .github/workflows/send-daily-changes.yml
          = 00:30 UTC (Tue-Sat).

The grouping mirrors the convention already used in commit subjects:
  'takeoff:'        -> AI Takeoff
  'bay-bid-list:'   -> Bay PowerBid
  'admin:'          -> Admin
  'watcher:' / 'bid setup:' / 'sbx-details:' / 'email policies:' -> Bid Setup
  'field:' / 'pm:'  -> Other panels
  everything else   -> Other
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


REPOS = [
    ("Portal", "alex-fusionelectric/fusion-bid-list"),
    ("Cron",   "alex-fusionelectric/fusion-cloud-cron"),
]
LOOKBACK_HOURS = 24

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


# --- Area inference from commit subject ---------------------------------

AREA_RULES = [
    ("AI Takeoff",        re.compile(r"^takeoff\b", re.I)),
    ("Bay PowerBid",      re.compile(r"^bay-bid-list\b", re.I)),
    ("Admin",             re.compile(r"^admin\b", re.I)),
    ("Bid Setup",         re.compile(r"^(bid setup|bid_setup|watcher|sbx-details|email polic|setup-bid|bids-data|main panel)\b", re.I)),
    ("Addenda",           re.compile(r"^(addenda|detect-bid-addenda)\b", re.I)),
    ("Chat",              re.compile(r"^(fusion-chat|chat)\b", re.I)),
    ("Field Panel",       re.compile(r"^(field|roster)\b", re.I)),
    ("PM Panel",          re.compile(r"^pm\b", re.I)),
    ("PlanSwift",         re.compile(r"^(planswift|plan-swift)\b", re.I)),
    ("Vendor quotes",     re.compile(r"^(vendor.?quote|quote.?tracker)\b", re.I)),
    ("Quote tracker",     re.compile(r"^quote", re.I)),
    ("Dependencies",      re.compile(r"^(requirements|deps|dep)\b", re.I)),
]
DEFAULT_AREA = "Other"


def classify_commit(subject: str) -> str:
    s = (subject or "").strip()
    for area, rx in AREA_RULES:
        if rx.match(s):
            return area
    return DEFAULT_AREA


# --- GitHub API ---------------------------------------------------------

def fetch_commits(repo_full_name: str, since_iso: str) -> list[dict]:
    """Pull commits for a repo since the given ISO timestamp.
    Uses unauthenticated GitHub API (60 reqs/hr) which is plenty for our
    daily cadence; bumps to 5000/hr if GH_TOKEN is provided."""
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "fusion-daily-changes"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = (f"https://api.github.com/repos/{repo_full_name}/commits"
           f"?since={urllib.parse.quote(since_iso)}&per_page=100")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")) or []
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[warn] GitHub fetch failed for {repo_full_name}: {e}", file=sys.stderr)
        return []


# --- HTML render --------------------------------------------------------

def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_email(grouped: dict, total_count: int, since_date: str) -> tuple[str, str]:
    """Returns (plain_text, html). Matches the bid-setup email's visual
    style so all four notification types feel like one family."""
    plain_lines = [f"Fusion Portal -- what shipped on {since_date}",
                   f"{total_count} commit(s) across {len(REPOS)} repos.", ""]

    sections_html = ""
    for area in sorted(grouped.keys()):
        commits = grouped[area]
        if not commits:
            continue
        plain_lines.append(f"## {area}")
        items_html = ""
        for c in commits:
            subj = c["subject"]
            sha7 = c["sha"][:7]
            repo_label = c["repo_label"]
            plain_lines.append(f"  - [{repo_label}] {sha7}  {subj}")
            items_html += (
                f'<li style="margin-bottom:6px;"><span style="font-family:monospace;color:#475569;font-size:11px;">'
                f'[{esc(repo_label)}] {esc(sha7)}</span> '
                f'<span style="color:#0f172a;">{esc(subj)}</span></li>'
            )
        plain_lines.append("")
        sections_html += (
            f'<div style="margin-bottom:18px;">'
            f'<div style="font-size:11px;font-weight:900;color:#7dd3fc;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px;">{esc(area)} &middot; {len(commits)}</div>'
            f'<ul style="margin:0;padding-left:18px;line-height:1.5;font-size:13px;">{items_html}</ul>'
            f'</div>'
        )
    if not sections_html:
        sections_html = '<div style="font-size:13px;color:#64748b;padding:14px 0;">No commits landed in the last 24 hours.</div>'

    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
<div style="max-width:640px;margin:24px auto;">
  <div style="background:#0f172a;padding:22px 28px;border-radius:10px 10px 0 0;">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#475569;margin-bottom:6px;">Fusion Portal &middot; Daily Update</div>
    <div style="font-size:22px;font-weight:900;color:#f8fafc;">What shipped {esc(since_date)}</div>
    <div style="font-size:13px;color:#7dd3fc;margin-top:4px;">{total_count} commit{'s' if total_count != 1 else ''} across {len(REPOS)} repos</div>
  </div>
  <div style="background:#fff;color:#0f172a;padding:22px 28px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 10px 10px;">
    {sections_html}
    <div style="border-top:1px solid #e2e8f0;padding-top:12px;margin-top:8px;font-size:11px;color:#64748b;">
      Per email_policies_cloud.daily_changes &middot;
      manage recipients at <a href="https://fusion-main-panel.netlify.app/admin/#email">admin/Email Center</a>.
    </div>
  </div>
</div>
</body></html>"""
    return "\n".join(plain_lines), html


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
    print(f"=== send-daily-changes started at {datetime.now(timezone.utc).isoformat()} ===")

    recipients = get_recipients_for("daily_changes")
    if not recipients:
        print("  [policy] daily_changes is disabled OR has no recipients -- skipping send.")
        return
    print(f"  [policy] recipients = {recipients}")

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    since_iso = since.isoformat(timespec="seconds")

    all_commits = []
    for label, repo in REPOS:
        commits = fetch_commits(repo, since_iso)
        for c in commits:
            subject = (c.get("commit", {}).get("message") or "").split("\n", 1)[0]
            all_commits.append({
                "repo_label": label,
                "sha":     c.get("sha", ""),
                "subject": subject,
                "area":    classify_commit(subject),
            })

    print(f"  pulled {len(all_commits)} commit(s) total across {len(REPOS)} repos")

    grouped: dict[str, list[dict]] = {}
    for c in all_commits:
        grouped.setdefault(c["area"], []).append(c)

    sender = (os.environ.get("GMAIL_FROM") or "alex@fusionelectric-inc.com").strip()

    # Local date label for the subject and header
    local = datetime.now()  # workflow runs in UTC; this is UTC-local but the date is close enough
    date_label = local.strftime("%-m/%-d/%Y") if hasattr(local, "strftime") else str(local.date())

    plain, html = render_email(grouped, len(all_commits), date_label)
    subject = f"Fusion Portal -- what shipped {date_label} ({len(all_commits)} commits)"

    if not all_commits:
        # We still send a "no changes today" email so recipients know the
        # cron ran and didn't silently fail. Keeps the heartbeat visible.
        plain += "\n(No commits landed in the last 24 hours.)"

    try:
        svc = gmail_service()
        send_email(svc, sender, recipients, subject, plain, html)
        record_send("daily_changes", recipients)
        print(f"  [sent] daily changes -> {recipients}")
    except Exception as e:  # noqa: BLE001
        print(f"[err] gmail send failed: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
