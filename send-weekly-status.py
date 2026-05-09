#!/usr/bin/env python3
"""send-weekly-status.py

Sends Alex a Sunday-evening summary of the week:
  - Hours per employee (PM-approved + still-pending)
  - Hours per project
  - Active jobs without addresses (map gap)
  - Bids in BIDDING / BID OR BAIL with no Setup Bid yet
  - Pending addenda (project has SBX-detected addenda not in folder)

Recipient: alex@fusionelectric-inc.com only (per current policy).

Runs Sunday 6 PM Pacific = Monday 02:00 UTC.

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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
except ImportError as exc:
    print(f"[error] google api libs missing: {exc}", file=sys.stderr)
    sys.exit(2)

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
RECIPIENT = "alex@fusionelectric-inc.com"


def _service_key() -> str:
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not k: raise SystemExit("SUPABASE_SERVICE_KEY env var required.")
    return k


def _sb(method, path, body=None, extra=None, timeout=30):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {"apikey": _service_key(), "Authorization": f"Bearer {_service_key()}", "content-type": "application/json"}
    if extra: headers.update(extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def gmail_service():
    raw = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if not raw: raise SystemExit("GMAIL_TOKEN_JSON env var required.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_json(path: str):
    st, body = _sb("GET", path)
    if st != 200: return []
    return json.loads(body)


def gather_week():
    """Pull all the data points the email needs in one pass."""
    today = datetime.now(timezone.utc).date()
    # Last 7 days = Sunday-to-Sunday is roughly the week we just finished
    since = (today - timedelta(days=7)).isoformat()
    until = today.isoformat()

    entries = fetch_json(
        f"field_time_entries_cloud?select=user_id,display_name,date,project_est,time_in,time_out,lunch_minutes,approved_at"
        f"&date=gte.{since}&date=lte.{until}&time_out=not.is.null"
    )
    active_projects = fetch_json(
        "projects_cloud?select=full_number,project_name,division,payload"
    )
    addresses = fetch_json("project_locations_cloud?select=full_number,lat,lng")
    bids_pending_setup = fetch_json(
        "bids_cloud?select=est_number,project_name,status&status=in.(BIDDING,BID%20OR%20BAIL)"
    )
    prebids = fetch_json("prebid_bids_cloud?select=est_number,status")
    addenda = fetch_json("bid_addenda_cloud?select=est_number,addendum_number,found_in_folder,auto_downloaded_at")
    # Budgets feed the per-division health roll-up. We pull only the
    # columns we aggregate on to keep the payload small for Claude.
    budgets = fetch_json(
        "budgets_cloud?select=full_number,project_name,division,project_manager,"
        "contract_total,est_profit,est_profit_pct,pct_complete,health,payload"
    )

    return {
        "since": since, "until": until,
        "entries": entries,
        "active_projects": active_projects,
        "addresses": addresses,
        "bids_pending_setup": bids_pending_setup,
        "prebids": prebids,
        "addenda": addenda,
        "budgets": budgets,
    }


def aggregate_division_health(budgets: list) -> dict:
    """Group active budgets by division and roll up the headline metrics
    + the few jobs the OPM should look at this week."""
    out = {}
    for b in budgets:
        pl = b.get("payload") or {}
        # Skip closed/completed jobs — only report on what's in flight.
        if str(pl.get("completeFlag", "")).upper() == "YES":
            continue
        if (b.get("pct_complete") or 0) >= 1.0:
            continue
        div = (b.get("division") or "").upper().strip() or "OTHER"
        d = out.setdefault(div, {
            "jobs": [], "contract_total": 0.0, "est_profit": 0.0,
            "good": 0, "watch": 0, "bad": 0, "watch_jobs": [],
        })
        ct = b.get("contract_total") or 0.0
        ep = b.get("est_profit") or 0.0
        d["jobs"].append(b)
        d["contract_total"] += ct
        d["est_profit"]     += ep
        h = (b.get("health") or "").lower()
        if h in ("good", "watch", "bad"):
            d[h] += 1
        if h in ("watch", "bad"):
            d["watch_jobs"].append({
                "full_number":  b.get("full_number"),
                "project_name": b.get("project_name"),
                "pm":           b.get("project_manager"),
                "health":       h,
                "contract":     ct,
                "est_profit_pct": b.get("est_profit_pct") or 0.0,
                "action_items": (pl.get("actionItems") or "").strip(),
            })
    # Drop empty divisions (e.g. one-off rows with no division + $0 contract)
    out = {div: d for div, d in out.items()
           if d["contract_total"] > 0 and div in ("BAY", "SAC")}
    # Margin + sort watch jobs by contract size (biggest first)
    for div, d in out.items():
        d["margin_pct"] = (d["est_profit"] / d["contract_total"]) if d["contract_total"] else 0.0
        d["watch_jobs"].sort(key=lambda j: -(j["contract"] or 0))
    return out


def claude_division_summary(div_health: dict) -> str | None:
    """Send the per-division roll-up to Claude Haiku for a 2-3 sentence
    executive narrative. Returns None if no API key or call fails — the
    email still ships, just without the narrative."""
    api_key = (os.environ.get("CLAUDE_API_KEY") or "").strip()
    if not api_key:
        return None
    if not div_health:
        return None

    # Compact prompt — give Claude the numbers, ask for plain prose.
    payload = []
    for div, d in sorted(div_health.items()):
        top_watch = d["watch_jobs"][:3]
        payload.append({
            "division":      div,
            "jobs_active":   len(d["jobs"]),
            "contract_$M":   round(d["contract_total"] / 1_000_000, 2),
            "est_profit_$":  round(d["est_profit"]),
            "margin_pct":    round(d["margin_pct"] * 100, 1),
            "good_count":    d["good"],
            "watch_count":   d["watch"],
            "bad_count":     d["bad"],
            "top_watch_jobs": [{
                "job":       j["full_number"],
                "name":      j["project_name"],
                "pm":        j["pm"],
                "health":    j["health"],
                "margin_pct": round((j["est_profit_pct"] or 0) * 100, 1),
                "issue":     j["action_items"][:120] or None,
            } for j in top_watch],
        })

    prompt = (
        "You are an electrical-contracting operations manager writing the "
        "weekly division-health note for the OPM (operations PM). Tone: "
        "direct, plain English, no fluff, no hype. 2 short paragraphs MAX, "
        "one per division. Lead with the overall picture, then call out "
        "the 1-2 jobs to watch by name. Skip the obvious (\"X jobs are good\"). "
        "Use numbers sparingly. End each paragraph with one concrete thing "
        "to look at this week.\n\n"
        f"Division roll-up:\n{json.dumps(payload, indent=2)}"
    )
    body_bytes = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body_bytes, method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  [claude-warn] {e}")
        return None
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text").strip()
    return text or None


def hours(ms: int) -> str:
    return f"{max(0, ms) / 3600000:.1f}h"


def money_short(n: float) -> str:
    """$2.4M / $385K / $920 — pick the unit that reads cleanly."""
    n = float(n or 0)
    if abs(n) >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if abs(n) >= 1_000:     return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


def _pretty_date_range(since: str, until: str) -> str:
    """e.g. 'May 4 — May 11, 2026'. %-d is *nix only; Windows needs %#d.
    The cron runs on Ubuntu, but using day-of-month directly keeps the
    script runnable on either platform for local previews."""
    s = datetime.fromisoformat(since); u = datetime.fromisoformat(until)
    def fmt(d, with_year=False):
        return f"{d.strftime('%b')} {d.day}" + (f", {d.year}" if with_year else "")
    if s.year == u.year:
        return f"{fmt(s)} – {fmt(u, True)}"
    return f"{fmt(s, True)} – {fmt(u, True)}"


def render_email(data: dict) -> tuple[str, str]:
    entries = data["entries"]
    # By employee
    by_emp = defaultdict(lambda: {"name": "", "hrs_ms": 0, "approved_ms": 0, "n": 0})
    for e in entries:
        if not e.get("time_in") or not e.get("time_out"): continue
        ms = (datetime.fromisoformat(e["time_out"].replace("Z","+00:00")).timestamp() -
              datetime.fromisoformat(e["time_in"].replace("Z","+00:00")).timestamp()) * 1000
        ms -= (e.get("lunch_minutes") or 0) * 60000
        rec = by_emp[e["user_id"]]
        rec["name"] = e.get("display_name") or e["user_id"]
        rec["hrs_ms"] += ms
        rec["n"] += 1
        if e.get("approved_at"): rec["approved_ms"] += ms

    # By project
    by_proj = defaultdict(lambda: {"hrs_ms": 0})
    for e in entries:
        if not e.get("time_in") or not e.get("time_out"): continue
        ms = (datetime.fromisoformat(e["time_out"].replace("Z","+00:00")).timestamp() -
              datetime.fromisoformat(e["time_in"].replace("Z","+00:00")).timestamp()) * 1000
        ms -= (e.get("lunch_minutes") or 0) * 60000
        key = e.get("project_est") or "Non-billable"
        by_proj[key]["hrs_ms"] += ms

    # Active jobs without addresses
    addr_set = {a["full_number"] for a in data["addresses"] if a.get("lat") is not None}
    active_no_addr = []
    for p in data["active_projects"]:
        pl = p.get("payload") or {}
        jls = (pl.get("jobListStatus") or "").upper()
        if jls in ("CURRENT", "READY TO CLOSE", "NO LABOR YET") and p["full_number"] not in addr_set:
            active_no_addr.append(p)

    # Bids in BIDDING/BID OR BAIL with no prebid yet (= not setup-bid'd yet)
    prebid_ests = {p["est_number"] for p in data["prebids"]}
    bids_no_setup = [b for b in data["bids_pending_setup"] if b["est_number"] not in prebid_ests]

    # Addenda missing from folder
    addenda_missing = [a for a in data["addenda"] if not a.get("found_in_folder")]

    total_hrs_ms = sum(r["hrs_ms"] for r in by_emp.values())
    approved_pct = (sum(r["approved_ms"] for r in by_emp.values()) / total_hrs_ms * 100
                    if total_hrs_ms > 0 else 0)

    # Division health roll-up + AI narrative. The narrative is a nice-to-have:
    # if Claude or budgets are unavailable the block degrades to numbers only.
    div_health = aggregate_division_health(data.get("budgets") or [])
    div_narrative = claude_division_summary(div_health) if div_health else None

    # Plain text body
    plain = []
    plain.append(f"Fusion weekly status — {data['since']} → {data['until']}\n")
    plain.append(f"TOTAL HOURS: {hours(total_hrs_ms)} ({approved_pct:.0f}% PM-approved)\n")
    plain.append("\n— HOURS BY EMPLOYEE —")
    for uid, r in sorted(by_emp.items(), key=lambda kv: -kv[1]["hrs_ms"]):
        approved = hours(r["approved_ms"])
        total = hours(r["hrs_ms"])
        pct = int(r["approved_ms"] / r["hrs_ms"] * 100) if r["hrs_ms"] else 0
        plain.append(f"  {r['name']:30s} {total} ({approved} approved, {pct}%)")
    plain.append("\n— HOURS BY PROJECT —")
    for proj, r in sorted(by_proj.items(), key=lambda kv: -kv[1]["hrs_ms"])[:15]:
        plain.append(f"  {proj:30s} {hours(r['hrs_ms'])}")
    if div_health:
        plain.append("\n— DIVISION HEALTH —")
        for div, d in sorted(div_health.items()):
            plain.append(
                f"  {div}: {len(d['jobs'])} active · {money_short(d['contract_total'])} contract · "
                f"{d['margin_pct']*100:.1f}% margin · "
                f"{d['good']} good / {d['watch']} watch / {d['bad']} bad"
            )
        if div_narrative:
            plain.append("\n  AI READ:")
            for line in div_narrative.split("\n"):
                plain.append(f"    {line}")
    plain.append(f"\n— GAPS —")
    plain.append(f"  Active jobs without an address: {len(active_no_addr)}")
    plain.append(f"  Bids awaiting Setup Bid:        {len(bids_no_setup)}")
    plain.append(f"  Addenda missing from folder:    {len(addenda_missing)}")
    plain_body = "\n".join(plain)

    # ──────────────────────────────────────────────────────────────────
    # HTML — elegant, professional, mobile-first.
    # Design: light card-on-light layout with generous whitespace,
    # restrained type scale, the headline number as a hero, employee
    # bars instead of dense tables. Inline styles only (email-safe).
    # ──────────────────────────────────────────────────────────────────
    def esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    pretty_range = _pretty_date_range(data["since"], data["until"])

    # Employee bars — top 8, biggest first, hour bars sized to max
    sorted_emp = sorted(by_emp.items(), key=lambda kv: -kv[1]["hrs_ms"])[:8]
    max_hrs_ms = max((r["hrs_ms"] for _, r in sorted_emp), default=1) or 1
    emp_html = ""
    for _, r in sorted_emp:
        pct = int(r["hrs_ms"] / max_hrs_ms * 100)
        approved_pct_emp = int(r["approved_ms"] / r["hrs_ms"] * 100) if r["hrs_ms"] else 0
        emp_html += f"""<tr>
<td style="padding:10px 0 4px;font-size:14px;color:#0f172a;font-weight:600">{esc(r['name'])}</td>
<td style="padding:10px 0 4px;font-size:13px;color:#0f172a;text-align:right;font-variant-numeric:tabular-nums;font-weight:700">{hours(r['hrs_ms'])}</td>
</tr>
<tr><td colspan="2" style="padding:0 0 10px">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse">
<tr>
<td height="6" style="background:#e2e8f0;border-radius:3px;width:100%;line-height:1">
<table cellpadding="0" cellspacing="0" border="0" width="{pct}%" style="height:6px"><tr><td height="6" style="background:#10b981;border-radius:3px;line-height:1">&nbsp;</td></tr></table>
</td>
</tr>
<tr><td style="padding-top:4px;color:#64748b;font-size:11px;letter-spacing:.3px">{approved_pct_emp}% approved · {hours(r['approved_ms'])}</td></tr>
</table>
</td></tr>"""

    # Project rows — top 8
    sorted_proj = sorted(by_proj.items(), key=lambda kv: -kv[1]["hrs_ms"])[:8]
    proj_html = ""
    for proj, r in sorted_proj:
        proj_html += f"""<tr>
<td style="padding:9px 0;border-top:1px solid #f1f5f9;font-size:14px;color:#0f172a">{esc(proj)}</td>
<td style="padding:9px 0;border-top:1px solid #f1f5f9;font-size:14px;color:#0f172a;text-align:right;font-weight:700;font-variant-numeric:tabular-nums">{hours(r['hrs_ms'])}</td>
</tr>"""

    # Gaps — three pills
    def gap_pill(count, label, color):
        return f"""<td valign="top" style="padding:6px;text-align:center;width:33.33%">
<div style="background:{color['bg']};border:1px solid {color['border']};border-radius:10px;padding:14px 8px">
<div style="font-size:24px;font-weight:800;color:{color['text']};line-height:1">{count}</div>
<div style="font-size:11px;color:#475569;margin-top:6px;line-height:1.3">{label}</div>
</div>
</td>"""
    color_red    = {"bg":"#fef2f2","border":"#fecaca","text":"#dc2626"}
    color_amber  = {"bg":"#fffbeb","border":"#fde68a","text":"#d97706"}
    color_blue   = {"bg":"#eff6ff","border":"#bfdbfe","text":"#2563eb"}

    # Drilldown details (collapsed)
    no_addr_li = "".join(
        f"<li style='padding:4px 0;color:#475569;font-size:13px'>{esc(p['full_number'])} <span style='color:#94a3b8'>·</span> {esc(p.get('project_name') or '')}</li>"
        for p in active_no_addr[:15]
    )
    no_setup_li = "".join(
        f"<li style='padding:4px 0;color:#475569;font-size:13px'>EST# {esc(b['est_number'])} <span style='color:#94a3b8'>·</span> {esc(b.get('project_name') or '')}</li>"
        for b in bids_no_setup[:15]
    )

    # Headline framing
    pending_count = sum(r["n"] for r in by_emp.values()) - sum(1 for e in entries if e.get("approved_at"))

    # ── Division health block ─────────────────────────────────────────
    # Per-division stats (contract $, margin %, health counts) plus
    # the AI narrative below them. Skip the whole block if there are
    # no active budgets at all.
    div_html = ""
    if div_health:
        div_cards = ""
        for div, d in sorted(div_health.items()):
            health_pills = (
                f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                f'background:#dcfce7;color:#15803d;font-size:11px;font-weight:700;margin-right:4px">{d["good"]} good</span>'
                f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                f'background:#fef3c7;color:#a16207;font-size:11px;font-weight:700;margin-right:4px">{d["watch"]} watch</span>'
                + (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                   f'background:#fee2e2;color:#b91c1c;font-size:11px;font-weight:700">{d["bad"]} bad</span>'
                   if d["bad"] else "")
            )
            margin = d["margin_pct"] * 100
            margin_color = "#10b981" if margin >= 18 else ("#f59e0b" if margin >= 10 else "#dc2626")
            top_watch = d["watch_jobs"][:2]
            watch_html = ""
            if top_watch:
                watch_html = '<div style="margin-top:10px;padding-top:10px;border-top:1px dashed #e2e8f0">'
                for j in top_watch:
                    watch_html += (
                        f'<div style="font-size:12px;color:#475569;padding:3px 0">'
                        f'<span style="color:#0f172a;font-weight:600">{esc(j["full_number"] or "")}</span> '
                        f'<span style="color:#94a3b8">·</span> {esc((j["project_name"] or "")[:36])}'
                        f'<span style="float:right;color:#dc2626;font-weight:700">{(j["est_profit_pct"] or 0)*100:.1f}%</span>'
                        f'</div>'
                    )
                watch_html += '</div>'
            div_cards += f"""<tr><td style="padding:0 0 12px">
<div style="border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;background:#fff">
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="font-size:13px;font-weight:800;letter-spacing:.6px;color:#0f172a">{esc(div)} DIVISION</td>
<td style="text-align:right;font-size:13px;color:#64748b">{len(d['jobs'])} active</td>
</tr></table>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:8px"><tr>
<td valign="top" style="font-size:11px;color:#94a3b8;letter-spacing:.5px">CONTRACT<br>
<span style="font-size:18px;font-weight:800;color:#0f172a;letter-spacing:-.5px">{esc(money_short(d['contract_total']))}</span></td>
<td valign="top" style="font-size:11px;color:#94a3b8;letter-spacing:.5px">MARGIN<br>
<span style="font-size:18px;font-weight:800;color:{margin_color};letter-spacing:-.5px">{margin:.1f}%</span></td>
<td valign="top" style="font-size:11px;color:#94a3b8;letter-spacing:.5px">EST PROFIT<br>
<span style="font-size:18px;font-weight:800;color:#0f172a;letter-spacing:-.5px">{esc(money_short(d['est_profit']))}</span></td>
</tr></table>
<div style="margin-top:10px">{health_pills}</div>
{watch_html}
</div>
</td></tr>"""

        narrative_html = ""
        if div_narrative:
            # Render the AI narrative as paragraphs (split on double newlines)
            paras = [p.strip() for p in div_narrative.split("\n\n") if p.strip()]
            paras_html = "".join(
                f'<p style="margin:0 0 10px;font-size:13px;line-height:1.55;color:#334155">{esc(p)}</p>'
                for p in paras
            )
            narrative_html = f"""<tr><td style="padding:6px 0 4px">
<div style="background:#f8fafc;border-left:3px solid #2563eb;border-radius:6px;padding:14px 16px">
<div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#2563eb;text-transform:uppercase;margin-bottom:8px">AI READ</div>
{paras_html}
</div>
</td></tr>"""

        div_html = f"""<tr><td style="padding:0 32px 18px">
<div style="font-size:11px;font-weight:700;letter-spacing:1.2px;color:#64748b;text-transform:uppercase;margin-bottom:10px">Division health</div>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse">
{div_cards}
{narrative_html}
</table>
</td></tr>"""

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;-webkit-font-smoothing:antialiased">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f1f5f9">
<tr><td align="center" style="padding:32px 16px">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,.08),0 4px 24px rgba(15,23,42,.04)">

<!-- Header strip -->
<tr><td style="padding:28px 32px 20px;border-bottom:1px solid #f1f5f9">
<div style="font-size:11px;font-weight:700;letter-spacing:1.5px;color:#64748b;text-transform:uppercase">Weekly status</div>
<div style="font-size:13px;color:#475569;margin-top:6px">{esc(pretty_range)}</div>
</td></tr>

<!-- Hero number -->
<tr><td style="padding:30px 32px 24px;text-align:center">
<div style="font-size:64px;line-height:1;font-weight:800;color:#0f172a;letter-spacing:-2px;font-variant-numeric:tabular-nums">{hours(total_hrs_ms).replace('h','')}<span style="font-size:24px;color:#94a3b8;font-weight:600;letter-spacing:0">h</span></div>
<div style="margin-top:10px;font-size:13px;color:#475569">total worked &nbsp;·&nbsp; <span style="color:#10b981;font-weight:600">{approved_pct:.0f}% PM-approved</span></div>
</td></tr>

<!-- Division health (per-division metrics + AI narrative) -->
{div_html}

<!-- Hours by employee -->
<tr><td style="padding:8px 32px 24px">
<div style="font-size:11px;font-weight:700;letter-spacing:1.2px;color:#64748b;text-transform:uppercase;margin-bottom:8px">Hours by employee</div>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse">
{emp_html or '<tr><td style="padding:14px 0;color:#94a3b8;font-size:13px;text-align:center">No hours logged this week.</td></tr>'}
</table>
</td></tr>

<!-- Hours by project -->
<tr><td style="padding:0 32px 24px">
<div style="font-size:11px;font-weight:700;letter-spacing:1.2px;color:#64748b;text-transform:uppercase;margin-bottom:4px">Top projects</div>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse">
{proj_html or '<tr><td style="padding:14px 0;color:#94a3b8;font-size:13px;text-align:center">No project hours.</td></tr>'}
</table>
</td></tr>

<!-- Gaps grid -->
<tr><td style="padding:8px 24px 24px;background:#fafbfc;border-top:1px solid #f1f5f9">
<div style="font-size:11px;font-weight:700;letter-spacing:1.2px;color:#64748b;text-transform:uppercase;margin:8px 8px 12px">Needs attention</div>
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
{gap_pill(len(active_no_addr), "Active jobs without an address", color_amber)}
{gap_pill(len(bids_no_setup), "Bids awaiting Setup Bid", color_blue)}
{gap_pill(len(addenda_missing), "Addenda missing from folder", color_red)}
</tr></table>
</td></tr>

<!-- Detail drilldowns (collapsed) -->
{f'''<tr><td style="padding:0 32px 16px"><details style="margin-top:8px"><summary style="cursor:pointer;color:#64748b;font-size:12px;font-weight:600;letter-spacing:.4px">Active jobs without address ({len(active_no_addr)})</summary><ul style="margin:8px 0 0;padding-left:18px;list-style:disc">{no_addr_li}</ul></details></td></tr>''' if no_addr_li else ''}
{f'''<tr><td style="padding:0 32px 16px"><details><summary style="cursor:pointer;color:#64748b;font-size:12px;font-weight:600;letter-spacing:.4px">Bids awaiting Setup Bid ({len(bids_no_setup)})</summary><ul style="margin:8px 0 0;padding-left:18px;list-style:disc">{no_setup_li}</ul></details></td></tr>''' if no_setup_li else ''}

<!-- Footer -->
<tr><td style="padding:20px 32px 24px;border-top:1px solid #f1f5f9;text-align:center">
<div style="font-size:11px;color:#94a3b8;letter-spacing:.4px">Fusion Electric &nbsp;·&nbsp; weekly digest &nbsp;·&nbsp; auto-generated Sunday 6 PM PT</div>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""
    return plain_body, html


def main():
    sender = (os.environ.get("GMAIL_FROM") or "").strip()
    if not sender: raise SystemExit("GMAIL_FROM env var required.")

    print("Gathering week data...")
    data = gather_week()
    plain, html = render_email(data)
    print(f"Sending to {RECIPIENT}...")

    svc = gmail_service()
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = RECIPIENT
    msg["Subject"] = f"Fusion weekly status — {data['since']} → {data['until']}"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    print("Sent.")


if __name__ == "__main__":
    main()
