"""send-bid-setup-notifications.py -- Picks up rows in
public.bid_setup_completions_cloud where notified_at is null, sends an
email manifest to the recipients listed on each row, and stamps notified_at.

The local watcher (bid_setup_watcher.py) writes one of these rows after
finishing autofill + plan/spec download for a bid. We don't send the email
from the watcher itself because Alex's PC may not have Gmail OAuth wired,
and the cron already does. Same OAuth + same Gmail-from as the job-walk
invite cron.

Required env: SUPABASE_SERVICE_KEY, GMAIL_TOKEN_JSON, GMAIL_FROM
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)


SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
COMPLETIONS_TABLE = "bid_setup_completions_cloud"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # canonical scope; subsumes send


def _service_key():
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k:
        raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb(method, path, body=None, extra=None, timeout=30):
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


def fetch_unnotified():
    qs = "select=*&notified_at=is.null&order=completed_at.asc&limit=50"
    st, body = _sb("GET", f"{COMPLETIONS_TABLE}?{qs}")
    if st != 200:
        raise SystemExit(f"completions GET failed: HTTP {st} {body[:200]!r}")
    return json.loads(body)


def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw:
        raise SystemExit("GMAIL_TOKEN_JSON env var required.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise SystemExit("Gmail credentials invalid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def render_email_body(row: dict) -> tuple[str, str]:
    """Return (plain_text, html) bodies."""
    est = row.get("est_number") or "?"
    name = row.get("project_name") or "(unnamed)"
    folder = row.get("folder_path") or "(unknown)"
    bb = row.get("bid_breakdown_path") or "(not autofilled)"
    filled = row.get("autofill_filled") or 0
    warns = row.get("autofill_warnings") or []
    dl_attempted = row.get("download_attempted")
    dl_ok = row.get("download_succeeded")
    dl_files = row.get("download_files_added") or 0
    manifest = row.get("download_manifest") or []

    download_block = ""
    if dl_attempted:
        if dl_ok and dl_files:
            files_list = "\n".join(f"  - {p}" for p in manifest[:50])
            more = f"\n  (+ {len(manifest) - 50} more)" if len(manifest) > 50 else ""
            download_block = (
                f"\nPLANS & SPECS pulled via OPS Downloader: "
                f"{dl_files} new file(s)\n{files_list}{more}\n"
            )
        elif dl_ok and not dl_files:
            download_block = "\nPLANS & SPECS check: package already up to date (0 new files).\n"
        else:
            download_block = (
                "\nPLANS & SPECS download: did not complete -- OPS Downloader "
                "may not have launched, or the package didn't land in Downloads "
                "within 10 minutes. Manual pull recommended.\n"
            )

    warn_block = ""
    if warns:
        warn_block = "\nWarnings:\n" + "\n".join(f"  - {w}" for w in warns) + "\n"

    plain = (
        f"Bid setup complete\n"
        f"==================\n"
        f"\n"
        f"EST #:       {est}\n"
        f"Project:     {name}\n"
        f"Estimator:   {row.get('estimator') or '-'}\n"
        f"Engineer:    {row.get('project_engineer') or '-'}\n"
        f"GC / Client: {row.get('client_gc') or '-'}\n"
        f"\n"
        f"Folder:         {folder}\n"
        f"Bid Breakdown:  {bb}\n"
        f"Cells filled:   {filled}\n"
        f"{warn_block}"
        f"{download_block}"
        f"\n"
        f"-- Bay PowerBid bid-setup watcher\n"
    )

    # Lightweight HTML version (Gmail prefers multipart/alternative)
    def esc(s):
        return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    files_html = ""
    if dl_attempted and dl_ok and dl_files:
        items = "".join(f"<li><code>{esc(p)}</code></li>" for p in manifest[:50])
        more = f"<li><i>+ {len(manifest) - 50} more</i></li>" if len(manifest) > 50 else ""
        files_html = (
            f"<p><b>PLANS &amp; SPECS pulled:</b> {dl_files} new file(s)</p>"
            f"<ul>{items}{more}</ul>"
        )
    elif dl_attempted and dl_ok and not dl_files:
        files_html = "<p><b>PLANS &amp; SPECS:</b> already up to date.</p>"
    elif dl_attempted:
        files_html = "<p><b>PLANS &amp; SPECS download:</b> did not complete. Manual pull recommended.</p>"

    warn_html = ""
    if warns:
        warn_html = "<p><b>Warnings:</b><ul>" + "".join(f"<li>{esc(w)}</li>" for w in warns) + "</ul></p>"

    html = f"""<!doctype html><html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#1f2937;">
<h2 style="margin:0 0 12px;color:#0f172a;">Bid setup complete</h2>
<table cellpadding="4" style="border-collapse:collapse;font-size:14px;">
  <tr><td style="color:#64748b;width:140px;">EST #</td><td><b>{esc(est)}</b></td></tr>
  <tr><td style="color:#64748b;">Project</td><td><b>{esc(name)}</b></td></tr>
  <tr><td style="color:#64748b;">Estimator</td><td>{esc(row.get('estimator') or '-')}</td></tr>
  <tr><td style="color:#64748b;">Engineer</td><td>{esc(row.get('project_engineer') or '-')}</td></tr>
  <tr><td style="color:#64748b;">GC / Client</td><td>{esc(row.get('client_gc') or '-')}</td></tr>
  <tr><td style="color:#64748b;">Folder</td><td><code>{esc(folder)}</code></td></tr>
  <tr><td style="color:#64748b;">Bid Breakdown</td><td><code>{esc(bb)}</code></td></tr>
  <tr><td style="color:#64748b;">Cells filled</td><td>{filled}</td></tr>
</table>
{warn_html}
{files_html}
<p style="color:#64748b;font-size:12px;margin-top:18px;">- Bay PowerBid bid-setup watcher</p>
</body></html>"""

    return plain, html


def send_notification(svc, sender: str, row: dict) -> None:
    recipients = [r for r in (row.get("notify_recipients") or []) if r]
    if not recipients:
        recipients = [sender]
    plain, html = render_email_body(row)
    subject = f"Bid setup complete: EST# {row.get('est_number') or '?'} {row.get('project_name') or ''}"
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def mark_notified(row_id: str) -> None:
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {"notified_at": iso, "updated_at": iso}
    st, resp = _sb(
        "PATCH",
        f"{COMPLETIONS_TABLE}?id=eq.{urllib.parse.quote(row_id, safe='')}",
        body=body,
    )
    if st not in (200, 204):
        print(f"  [warn] notified mark failed HTTP {st}: {resp[:200]!r}")


def main():
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender:
        raise SystemExit("GMAIL_FROM env var required.")
    rows = fetch_unnotified()
    print(f"Unnotified completion rows: {len(rows)}")
    if not rows:
        return
    svc = gmail_service()
    for r in rows:
        try:
            send_notification(svc, sender, r)
            mark_notified(r["id"])
            print(f"  notified {r.get('est_number')} -> {len(r.get('notify_recipients') or [])} recipient(s)")
        except Exception as e:  # noqa: BLE001
            print(f"  [err] {r.get('id')}: {e}")


if __name__ == "__main__":
    main()
