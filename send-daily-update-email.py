"""send-daily-update-email.py — Once-a-day digest email summarizing
every commit that landed today across the fusion-bid-list (Portal)
and fusion-cloud-cron repos.

Replaces the in-app "What's new today" toast — Alex wants this as
forwardable email so people who don't use the app can also see what
changed.

Schedule: nightly at 9:00 PM PT (= 04:00 UTC next day). Sends only
when there's at least one commit; silent otherwise. Sender +
recipients reuse the existing GMAIL_FROM / GMAIL_TOKEN_JSON conventions
from the other send-*.py scripts in this repo.

Required env:
  GMAIL_TOKEN_JSON
  GMAIL_FROM
  GH_TOKEN              (optional — needed only for the private
                         fusion-bid-list repo. fusion-cloud-cron is
                         public.)
  DAILY_UPDATE_RECIPIENTS  comma-separated email list (defaults to
                           alex@fusionelectric-inc.com when unset)
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

REPOS = [
    {"name": "fusion-bid-list",   "owner": "alex-fusionelectric", "label": "Portal"},
    {"name": "fusion-cloud-cron", "owner": "alex-fusionelectric", "label": "Cron"},
]

DEFAULT_RECIPIENTS = ["alex@fusionelectric-inc.com"]


# --- GitHub commits ---------------------------------------------------------

def github_request(url: str) -> dict | list | None:
    headers = {
        "Accept":      "application/vnd.github+json",
        "User-Agent":  "fusion-daily-update-email",
    }
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [gh-warn] {url}: HTTP {e.code} {e.read()[:160]!r}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  [gh-warn] {url}: {e}")
        return None


def fetch_repo_commits(owner: str, name: str) -> list[dict]:
    """Pull the most-recent 100 commits — plenty for a 24-hour window."""
    url = f"https://api.github.com/repos/{owner}/{name}/commits?per_page=100"
    data = github_request(url)
    return data if isinstance(data, list) else []


def is_today_pt(iso_str: str, today_pt: dt.date) -> bool:
    """Return True iff the ISO timestamp falls inside `today_pt` (PT)."""
    if not iso_str:
        return False
    try:
        d_utc = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    # Convert to a fixed-offset PT (naive about DST). Off by an hour
    # for ~2 weeks/year, which only matters within the first/last hour
    # of the day — close enough for a digest email.
    pt_offset = dt.timedelta(hours=-7)
    d_pt = (d_utc + pt_offset).date()
    return d_pt == today_pt


def collect_todays_commits() -> list[dict]:
    """Return [{sha, short, repo, repo_label, date, author, subject, url}, …]."""
    now_pt = (dt.datetime.utcnow() + dt.timedelta(hours=-7))
    today_pt = now_pt.date()
    out: list[dict] = []
    for r in REPOS:
        commits = fetch_repo_commits(r["owner"], r["name"])
        for c in commits:
            committed = (c.get("commit") or {}).get("committer") or {}
            iso = committed.get("date") or ""
            if not is_today_pt(iso, today_pt):
                continue
            author = (c.get("commit") or {}).get("author") or {}
            subj = ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0]
            out.append({
                "sha":   c.get("sha") or "",
                "short": (c.get("sha") or "")[:7],
                "repo":  r["label"],
                "date":  iso,
                "author": author.get("name") or "—",
                "subject": subj,
                "url":   c.get("html_url") or "",
            })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


# --- Email rendering --------------------------------------------------------

def render_email(commits: list[dict], today_pt: dt.date) -> tuple[str, str, str]:
    pretty = today_pt.strftime("%A, %b %-d, %Y") if hasattr(today_pt, "strftime") else str(today_pt)
    # Windows %-d isn't supported; fall back to %#d on Windows runners.
    try:
        pretty = today_pt.strftime("%A, %b %-d, %Y")
    except ValueError:
        pretty = today_pt.strftime("%A, %b %#d, %Y")
    subject = f"Fusion app updates — {today_pt.strftime('%m/%d/%Y')} · {len(commits)} change{'s' if len(commits) != 1 else ''}"

    # Plain-text body.
    lines = [
        f"Fusion app updates · {pretty}",
        "=" * 56,
        "",
        f"{len(commits)} change{'s' if len(commits) != 1 else ''} shipped to the Fusion suite today.",
        "",
    ]
    for c in commits:
        lines.append(f"[{c['repo'].upper():6}] {c['short']}  {c['subject']}")
    lines += [
        "",
        "—",
        "Forward freely to anyone on the team who isn't in the portal.",
        "Auto-generated by fusion-cloud-cron / send-daily-update-email.py",
    ]
    plain = "\n".join(lines)

    # HTML body — forwardable, dark-tolerant, no external assets.
    rows = []
    for c in commits:
        repo_color = "#4dabff" if c["repo"] == "Portal" else "#fbbf24"
        rows.append(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e4e4e7;vertical-align:top;">'
            f'  <span style="display:inline-block;padding:1px 8px;border-radius:999px;background:{repo_color}22;color:{repo_color};font-size:10px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;">{c["repo"]}</span>'
            f'</td><td style="padding:8px 12px;border-bottom:1px solid #e4e4e7;font-family:ui-monospace,Menlo,Consolas,monospace;color:#71717a;font-size:12px;vertical-align:top;">{c["short"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e4e4e7;vertical-align:top;color:#18181b;font-size:13px;line-height:1.5;">{_html_escape(c["subject"])}</td></tr>'
        )
    html = f"""<!doctype html>
<html><body style="margin:0;background:#f4f4f5;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;color:#18181b;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f4f5;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" style="max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.06);">
        <tr><td style="padding:22px 24px 8px 24px;">
          <div style="font-size:11px;font-weight:800;letter-spacing:.6px;text-transform:uppercase;color:#71717a;">FUSION ELECTRIC · DAILY UPDATES</div>
          <h1 style="margin:6px 0 4px;font-size:22px;font-weight:800;color:#18181b;">What shipped today</h1>
          <div style="font-size:13px;color:#52525b;">{pretty} · {len(commits)} change{'s' if len(commits) != 1 else ''}</div>
        </td></tr>
        <tr><td style="padding:8px 12px 0 12px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
            {''.join(rows)}
          </table>
        </td></tr>
        <tr><td style="padding:16px 24px 22px 24px;color:#71717a;font-size:11px;line-height:1.6;border-top:1px solid #e4e4e7;">
          Forward this to anyone on the team who doesn't use the portal directly. The full timeline (with code links) is in the Admin&nbsp;&rsaquo;&nbsp;Deploys tab.<br>
          <span style="opacity:.65;">Auto-generated by fusion-cloud-cron / send-daily-update-email.py</span>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    return subject, plain, html


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Gmail send -------------------------------------------------------------

def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(svc, sender: str, recipients: list[str], subject: str, plain: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


# --- Main -------------------------------------------------------------------

def main() -> int:
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender:
        raise SystemExit("GMAIL_FROM env var required.")
    rcpts = [
        e.strip()
        for e in (os.environ.get("DAILY_UPDATE_RECIPIENTS") or ",".join(DEFAULT_RECIPIENTS)).split(",")
        if e.strip()
    ] or DEFAULT_RECIPIENTS

    commits = collect_todays_commits()
    print(f"Today's commits across {len(REPOS)} repos: {len(commits)}")
    if not commits:
        print("No commits today — nothing to send.")
        return 0

    today_pt = (dt.datetime.utcnow() + dt.timedelta(hours=-7)).date()
    subject, plain, html = render_email(commits, today_pt)

    svc = gmail_service()
    send_email(svc, sender, rcpts, subject, plain, html)
    print(f"Sent: '{subject}' to {len(rcpts)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
