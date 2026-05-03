"""parse-gmail-quotes.py — headless Gmail-to-quote-tracker parser.

Reads vendor quote-request threads from Gmail labels under
`ESTIMATING/CURRENT BIDS/*` and emits the same HTML+JSON shape that the
Cowork project produces. Convert-QuoteTracker.ps1 then lifts the JSON out
and the BAY Bid List page renders it.

Authentication: OAuth 2.0 desktop flow.
  - First run pops a browser to authorize gmail.readonly scope.
  - Token is cached in scripts/gmail-token.json so subsequent runs are
    fully unattended (the scheduler can call this every 15 min).
  - If the token expires or scopes change, deletes scripts/gmail-token.json
    and re-runs the script interactively.

Rule-based parsing — no LLM. Trades smarts for $0/run cost.
  - Outbound vs inbound: sender domain matches USER_DOMAIN -> outbound.
  - Vendor: pulled from the inbound sender's display name or domain.
  - Scope: matched from outbound subject keywords (fire alarm / security /
    low voltage / lighting / distribution / trench / audio visual / nurse
    call). Anything else is bucketed as the raw subject snippet.
  - Response status:
      * Pending: no inbound message in thread.
      * Acknowledged: inbound exists but no attachment / no quote keyword.
      * Received: inbound has attachment OR body contains $ / quote /
        pricing / proposal / bid.
  - bid_due_date: cross-referenced from fusion-portal/src/assets/
    bids-data.json by project_number when available; otherwise null.

Usage:
  python parse-gmail-quotes.py --out "C:/.../Current Bids Tracker.html"

The output HTML is intentionally minimal — it only needs the
<script id="cb-data" type="application/json">…</script> block that
Convert-QuoteTracker.ps1 reads.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html as htmlmod
import json
import os
import re
import sys
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Anthropic is optional — when CLAUDE_API_KEY (or ANTHROPIC_API_KEY) is set
# we layer LLM enrichment on top of the rule-based parse. Without it the
# script falls back to deterministic-only output.
try:
    import anthropic  # type: ignore
except ImportError:
    anthropic = None  # noqa: F811

# ---- Config --------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRIPTS_DIR / "gmail-credentials.json"
TOKEN_PATH = SCRIPTS_DIR / "gmail-token.json"
# Per-project LLM enrichment cache. Keyed by project label + signature of
# the threads (so we only re-call Claude when something changed).
LLM_CACHE_PATH = SCRIPTS_DIR / "gmail-llm-cache.json"
# Tracks the last time we emailed the user about an Anthropic credit /
# billing wall, to avoid sending an alert every 15 minutes when the auto-
# update task hits the same error in a loop.
CREDIT_ALERT_STATE_PATH = SCRIPTS_DIR / "credit-alert-state.json"
CREDIT_ALERT_COOLDOWN_HOURS = 12
CREDIT_ALERT_TO = "alex@fusionelectric-inc.com"

# Claude config. Opus 4.7 is the default but Haiku 4.5 is way cheaper for
# this workload. Override via env var when you want to dial it.
CLAUDE_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
# 8K is enough for ~50 vendors of JSON output; under the 1024-output min
# for prompt caching is fine since we cache the system prompt only.
CLAUDE_MAX_TOKENS = 8000

# Gmail label hierarchy. Sub-labels under this prefix are treated as
# individual project folders.
ROOT_LABEL = "ESTIMATING/CURRENT BIDS"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    # gmail.send is used by send_credit_alert() to email the user when the
    # Anthropic account hits a credit-balance / billing wall mid-run.
    "https://www.googleapis.com/auth/gmail.send",
]

# Anything @ this domain is treated as outbound (us). Everything else is a
# vendor reply.
USER_DOMAIN = "fusionelectric-inc.com"

# Scope vocabulary — same buckets the BAY Bid List page renders. Order is
# fall-through priority: longer/more-specific keywords win.
SCOPE_RULES = [
    ("audio visual", "audio visual"),
    ("audio-visual", "audio visual"),
    (" a/v", "audio visual"),
    (" av ", "audio visual"),
    ("nurse call", "nurse call"),
    ("nurse-call", "nurse call"),
    ("fire alarm", "fire alarm"),
    ("fire-alarm", "fire alarm"),
    ("low voltage", "low voltage"),
    ("low-voltage", "low voltage"),
    ("security", "security"),
    ("access control", "security"),
    ("lighting", "lighting"),
    ("trench", "trenching"),
    ("underground", "trenching"),
    ("switchgear", "distribution"),
    ("distribut", "distribution"),
    # Integration / SCADA / pump-station controls — keyword-based fallback
    # that runs when no subject suffix or LLM classification is available.
    ("scada", "integration"),
    ("integration", "integration"),
    ("pump station", "integration"),
    ("lift station", "integration"),
    ("plc", "integration"),
]

# Body keywords that promote a reply from "Acknowledged" to "Received".
QUOTE_KEYWORDS = [
    "$", " quote", "pricing", "proposal", "bid", "estimate",
    "attached", "see attachment", "please find",
]


# ---- Gmail auth ----------------------------------------------------------

def get_service():
    """Return an authorized Gmail API service.

    First run: opens browser for OAuth consent. Subsequent runs: silently
    refreshes the cached token. If the token is broken (expired refresh
    token, scope change), delete gmail-token.json and re-auth interactively.
    """
    # Cloud path first: GMAIL_TOKEN_JSON env var contains the refresh
    # token, set in GitHub Actions secrets. When present we skip the
    # local token file entirely (runners have no $HOME state to read
    # from anyway) and skip the interactive InstalledAppFlow (no
    # browser available on a runner).
    creds = None
    token_json_env = (os.environ.get("GMAIL_TOKEN_JSON") or "").strip()
    if token_json_env:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json_env), SCOPES)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"GMAIL_TOKEN_JSON env var malformed: {exc}")
        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(f"cloud token refresh failed: {exc}")
        if not creds or not creds.valid:
            raise SystemExit("cloud OAuth creds invalid (env GMAIL_TOKEN_JSON)")
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # PC path: local file-based token + interactive bootstrap if needed.
    if not CREDENTIALS_PATH.is_file():
        raise SystemExit(f"Missing OAuth credentials: {CREDENTIALS_PATH}")

    if TOKEN_PATH.is_file():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] token refresh failed: {exc} — re-authorizing", file=sys.stderr)
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            # run_local_server opens a browser; for full unattended runs use
            # run_console() — but the desktop flow Google issues prefers a
            # local redirect, so stick with run_local_server.
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---- Parsers -------------------------------------------------------------

PROJECT_LABEL_RE = re.compile(r"^\s*\d{2}-\d{3,4}\b")

def list_project_labels(service):
    """Return [(label_id, label_name)] for active-bid labels under ROOT_LABEL.

    Only labels whose tail starts with a real project number (NN-NNN or
    NN-NNNN) are included. Buckets like "00-POTENTIAL BIDS" are skipped
    because they accumulate hundreds of un-prioritized vendor threads
    that would drown the active-bid view.
    """
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    out = []
    prefix = ROOT_LABEL + "/"
    for lab in labels:
        name = lab.get("name", "")
        if not (name.startswith(prefix) and len(name) > len(prefix)):
            continue
        tail = name[len(prefix):]
        if not PROJECT_LABEL_RE.match(tail):
            continue
        out.append((lab["id"], name))
    out.sort(key=lambda t: t[1])
    return out


def list_thread_ids_for_label(service, label_id):
    """Paginate through a label and return thread IDs."""
    out = []
    page_token = None
    while True:
        kwargs = dict(userId="me", labelIds=[label_id], maxResults=500)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().threads().list(**kwargs).execute()
        for t in resp.get("threads", []) or []:
            out.append(t["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def fetch_thread(service, thread_id):
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute()


def header(msg, name):
    for h in (msg.get("payload", {}).get("headers", []) or []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def message_body_text(payload):
    """Best-effort plain-text extract from a Gmail message payload."""
    if not payload:
        return ""
    if payload.get("body", {}).get("data"):
        try:
            return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            pass
    parts = payload.get("parts") or []
    text = []
    for p in parts:
        mime = p.get("mimeType", "")
        if mime == "text/plain" and p.get("body", {}).get("data"):
            try:
                text.append(base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                continue
        elif mime.startswith("multipart/"):
            text.append(message_body_text(p))
    if text:
        return "\n".join(text)
    # Fallback to HTML body, stripped of tags.
    for p in parts:
        if p.get("mimeType") == "text/html" and p.get("body", {}).get("data"):
            try:
                raw = base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", errors="ignore")
                return re.sub(r"<[^>]+>", " ", raw)
            except Exception:  # noqa: BLE001
                continue
    return ""


def has_attachment(payload):
    if not payload:
        return False
    fname = payload.get("filename") or ""
    if fname.strip():
        body = payload.get("body", {}) or {}
        if body.get("attachmentId") or body.get("size", 0) > 0:
            return True
    for p in payload.get("parts") or []:
        if has_attachment(p):
            return True
    return False


def parse_sender(raw):
    name, addr = parseaddr(raw or "")
    return name.strip(), addr.strip().lower()


def is_outbound(addr):
    return bool(addr) and addr.endswith("@" + USER_DOMAIN)


def parse_internal_date(msg):
    """Gmail internalDate is ms since epoch; safer than the Date: header."""
    d = msg.get("internalDate")
    if d:
        try:
            return dt.datetime.fromtimestamp(int(d) / 1000, tz=dt.timezone.utc)
        except Exception:  # noqa: BLE001
            pass
    raw = header(msg, "Date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(dt.timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    return None


def bucket_scope(text):
    """Initial scope guess from a subject string — used as a placeholder
    BEFORE Claude enrichment kicks in. Conservative on purpose: a subject
    like "Security Quote Request - 25-396 Bidwell Park" must NOT pre-tag
    the row as SEC because the actual delivered quote may be a different
    scope (e.g. Pavion responding with FA pricing). The cleaned_rows pass
    overrides this with Claude's body-based classification when available.
    Returns 'general' when in doubt."""
    s = " " + (text or "").lower() + " "
    # Only honor scope keywords when they appear as an explicit suffix tag
    # (after a " - ") — same rule the post-LLM subject hint uses.
    suffix_rules = [
        (" - fa ", "fire alarm"),
        (" - fire alarm ", "fire alarm"),
        (" - lv ", "low voltage"),
        (" - low voltage ", "low voltage"),
        (" - sec ", "security"),
        (" - security ", "security"),
        (" - ltng ", "lighting"),
        (" - lighting ", "lighting"),
        (" - dist ", "distribution"),
        (" - distribution ", "distribution"),
        (" - generator ", "distribution"),
        (" - switchgear ", "distribution"),
        (" - gear ", "distribution"),
        (" - trnch ", "trenching"),
        (" - trench ", "trenching"),
        (" - av ", "audio visual"),
        (" - a/v ", "audio visual"),
        (" - nc ", "nurse call"),
        (" - nurse call ", "nurse call"),
        (" - aic ", "integration"),
        (" - integration ", "integration"),
        (" - scada ", "integration"),
        (" - controls ", "integration"),
    ]
    for needle, label in suffix_rules:
        if needle in s:
            return label
    return "general"


# Subject-line abbreviation fallback. When Claude returns "general" we
# check if the request subject contains a clear scope hint (e.g. " - FA",
# " - SEC", " - Dist") and override. Estimators put these abbreviations
# in the subject deliberately — they're the most reliable scope signal.
SUBJECT_SUFFIX_HINTS = [
    (" - FA", "fire alarm"),
    (" -FA", "fire alarm"),
    (" - LV", "low voltage"),
    (" -LV", "low voltage"),
    (" - SEC", "security"),
    (" -SEC", "security"),
    (" - LTNG", "lighting"),
    (" - LIGHTING", "lighting"),
    (" - LIGHT", "lighting"),
    (" - DIST", "distribution"),
    (" -DIST", "distribution"),
    (" - DISTRIBUTION", "distribution"),
    (" - GENERATOR", "distribution"),  # generator scope is power-distribution adjacent
    (" - GENERATOR RENTAL", "distribution"),
    (" - SWITCHGEAR", "distribution"),
    (" - GEAR", "distribution"),
    (" - TRNCH", "trenching"),
    (" - TRENCH", "trenching"),
    (" - AV", "audio visual"),
    (" - A/V", "audio visual"),
    (" - NC", "nurse call"),
    (" - NURSE CALL", "nurse call"),
    (" - FIRE ALARM", "fire alarm"),
    (" - LOW VOLTAGE", "low voltage"),
    (" - SECURITY", "security"),
    # Integration / SCADA / controls — pump stations, water/wastewater.
    (" - AIC", "integration"),
    (" - INT", "integration"),
    (" - INTEGRATION", "integration"),
    (" - SCADA", "integration"),
    (" - CONTROLS", "integration"),
]

# Compound suffixes that map to multiple scopes (e.g. "- LV/SEC" implies BOTH).
COMPOUND_SUFFIX_TOKENS = {
    "FA": "fire alarm",
    "LV": "low voltage",
    "SEC": "security",
    "AV": "audio visual",
    "LTNG": "lighting",
    "LIGHTING": "lighting",
    "DIST": "distribution",
    "TRNCH": "trenching",
    "NC": "nurse call",
    "INT": "integration",
    "AIC": "integration",
    "SCADA": "integration",
}


def fallback_scopes_from_subject(subject):
    """Return a list of canonical scopes parsed from a quote-request subject.
    Empty list if no hint found. Handles compound subjects like
    'Bidwell Park - LV/SEC' (returns ['low voltage', 'security']).

    Estimators set these suffixes deliberately — they're the highest-trust
    scope signal. The LLM prompt also tells Claude to honor them, but the
    parser enforces it as a final safety net so subject suffixes always win
    over body content."""
    if not subject:
        return []
    s = " " + str(subject).upper() + " "
    # Compound: " - X/Y/Z " — explode into individual tokens.
    m = re.search(r" - ([A-Z/]+)(?:\s|$)", s)
    if m:
        tokens = [t.strip() for t in m.group(1).split("/") if t.strip()]
        out = []
        for t in tokens:
            if t in COMPOUND_SUFFIX_TOKENS:
                v = COMPOUND_SUFFIX_TOKENS[t]
                if v not in out:
                    out.append(v)
        if out:
            return out
    # Single hint match against the canonical suffix list.
    for hint, label in SUBJECT_SUFFIX_HINTS:
        if hint in s:
            return [label]
    # NOTE: We deliberately do NOT loose-match bare scope words. Subjects
    # like "Security Quote Request - 25-396 Bidwell Park Hayward" describe
    # what was REQUESTED — but the vendor's actual response may be a
    # different scope (e.g. Pavion delivered FA on a security request).
    # Trust Claude's body-based classification when the subject doesn't
    # carry an explicit "- SEC" / "- FA" suffix at the end.
    loose = [
        # Reserved for very-specific multi-word phrases that, by themselves,
        # are unambiguous scope tags. Currently empty — add cautiously.
        ("AUDIO VISUAL", "audio visual"),
    ]
    for needle, label in loose:
        if needle in s:
            return [label]
    return []


def fallback_scope_from_subject(subject):
    """Legacy single-scope helper — wraps fallback_scopes_from_subject and
    returns just the first hit (or None). Kept so callers that only need
    one scope still work without changing every call site."""
    arr = fallback_scopes_from_subject(subject)
    return arr[0] if arr else None


# Known vendor companies keyed by email domain. Adds two benefits:
#  1. The display name is consistent across multiple staff (Steven Lewis @
#     rfi.com and Kevin Tchang @ netronixint.com both show as "Pavion / RFi").
#  2. The dashboard reader instantly knows which parent company the email
#     came from, even when only the contact's name is visible.
DOMAIN_COMPANY_MAP = {
    "rfi.com": "Pavion / RFi",
    "pavion.com": "Pavion",
    "netronixint.com": "Netronix Integration",
    "ncvd.com": "NCVD",
    "sasco.com": "SASCO",
    "pcd-electric.com": "PCD",
    "graybar.com": "Graybar",
    "ced.com": "CED",
    "edges.com": "Edges Electrical",
    "edgesgroup.com": "Edges Electrical",
    "yourced.com": "CED",
    "jci.com": "Johnson Controls (JCI)",
    "johnsoncontrols.com": "Johnson Controls (JCI)",
    "pyrocomm.com": "Pyro-Comm",
    "247firealarm.com": "247 Fire Alarm",
    "bayalarm.com": "Bay Alarm",
    "mainelectricsupply.com": "Main Electric",
    "verkada.com": "Verkada",
    "lenel.com": "Lenel",
    "anixter.com": "Anixter",
    "structurednet.com": "StructureNet",
    "telstar-instruments.com": "Telstar Instruments",
    "tescocontrols.com": "Tesco Controls",
}


def vendor_company_from(name, addr):
    """Best-effort vendor company name. The DOMAIN_COMPANY_MAP wins over the
    From header's display name because a domain like @rfi.com is a more
    reliable parent-company signal than a person's name."""
    if addr and "@" in addr:
        domain = addr.split("@", 1)[1].lower()
        if domain in DOMAIN_COMPANY_MAP:
            return DOMAIN_COMPANY_MAP[domain]
    if name:
        # "Acme Corp <foo@acme.com>" → "Acme Corp"
        return name
    if addr and "@" in addr:
        domain = addr.split("@", 1)[1]
        # acme.com → Acme
        first = domain.split(".")[0]
        return first.replace("-", " ").title()
    return "Unknown"


def project_meta_from_label(label_name):
    """ESTIMATING/CURRENT BIDS/25-396 BIDWELL PARK HAYWARD →
       (project_number, project_name)."""
    tail = label_name.split("/", 2)[-1]
    m = re.match(r"^\s*(\d{2}-\d{3,4}|\d{4,5})\s+(.*)$", tail)
    if m:
        return m.group(1), m.group(2).title()
    # Fallback: whole tail as name, no number.
    return "", tail.title()


def load_bids_due_dates():
    """Cross-ref bid_due_date from the static portal data so dates match
    the rest of the dashboard. Maps "estNumber" or projectName to date."""
    data_path = SCRIPTS_DIR.parent / "src" / "assets" / "bids-data.json"
    if not data_path.is_file():
        return {}
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for b in payload.get("bids", []) or []:
        num = (b.get("estNumber") or "").strip()
        date = (b.get("bidDueDate") or "").strip()
        if num and date:
            out.setdefault(num, date)
    return out


# ---- Thread → vendor row -------------------------------------------------

def parse_thread(thread, today, bid_due_dates_by_num, project_number):
    """Reduce a thread to ONE vendor row (or None if it should be skipped).

    A thread = a request to a single vendor + their replies. We pick the
    earliest outbound message as the "request" and the latest inbound as
    the "last reply".
    """
    msgs = thread.get("messages", []) or []
    if not msgs:
        return None, "empty"

    # Sort by date so first/last selection is deterministic.
    sorted_msgs = sorted(msgs, key=lambda m: parse_internal_date(m) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))

    request_msg = None
    last_inbound = None
    inbound_count = 0
    received_quote = False

    for m in sorted_msgs:
        sender_raw = header(m, "From")
        name, addr = parse_sender(sender_raw)
        date = parse_internal_date(m)
        if is_outbound(addr):
            if request_msg is None:
                request_msg = m
            continue
        # Inbound (vendor)
        inbound_count += 1
        last_inbound = m
        body = (message_body_text(m.get("payload")) or "").lower()
        if has_attachment(m.get("payload")) or any(k in body for k in QUOTE_KEYWORDS):
            received_quote = True

    if request_msg is None:
        # Thread starts inbound (vendor cold-emailed us) — fall back to
        # treating the first inbound as the "request" so we still surface
        # something.
        request_msg = sorted_msgs[0]

    request_subject = header(request_msg, "Subject")
    request_date = parse_internal_date(request_msg)

    # Vendor identification — prefer the inbound reply's sender, fall back
    # to the recipient list of the outbound request.
    vendor_name = ""
    vendor_email = ""
    vendor_company = ""
    if last_inbound is not None:
        rname, raddr = parse_sender(header(last_inbound, "From"))
        vendor_name = rname
        vendor_email = raddr
        vendor_company = vendor_company_from(rname, raddr)
    else:
        # Pull first non-self recipient from To/Cc of the outbound request.
        recipients_raw = ", ".join(filter(None, [header(request_msg, "To"), header(request_msg, "Cc")]))
        for chunk in recipients_raw.split(","):
            cname, caddr = parse_sender(chunk)
            if caddr and not is_outbound(caddr):
                vendor_name = cname
                vendor_email = caddr
                vendor_company = vendor_company_from(cname, caddr)
                break

    if not vendor_email:
        # Not enough signal to call it a vendor thread.
        return None, "no vendor identified"

    scope = bucket_scope(request_subject)

    # Status
    if inbound_count == 0:
        status = "Pending"
    elif received_quote:
        status = "Received"
    else:
        status = "Acknowledged"

    request_date_iso = request_date.date().isoformat() if request_date else None
    last_inbound_iso = parse_internal_date(last_inbound).date().isoformat() if last_inbound else None

    days_since_request = (today - request_date.date()).days if request_date else None
    bid_due = bid_due_dates_by_num.get(project_number)
    days_until_bid = None
    if bid_due:
        try:
            d = dt.date.fromisoformat(bid_due[:10])
            days_until_bid = (d - today).days
        except Exception:  # noqa: BLE001
            pass

    # Follow-up flag: 5+ days since request and still no quote.
    follow_up_flag = bool(
        days_since_request is not None and days_since_request >= 5 and status != "Received"
    )

    vendor_row = {
        "scope": scope,
        "vendor_company": vendor_company,
        "vendor_contact_name": vendor_name or "Unknown",
        "vendor_contact_email": vendor_email or "Unknown",
        "request_thread_id": thread.get("id"),
        "request_subject": request_subject,
        "request_date": request_date_iso,
        "last_inbound_date": last_inbound_iso,
        "response_status": status,
        "days_since_request": days_since_request,
        "days_until_bid": days_until_bid,
        "follow_up_flag": follow_up_flag,
        "notes": "",  # Reserved — Cowork populated this; rule-based leaves blank.
    }
    return vendor_row, None


# ---- LLM enrichment (optional) ------------------------------------------

LLM_SYSTEM_PROMPT = (
    "You are the bidding-desk classifier for Fusion Electric, a Bay-area / "
    "Sacramento electrical contractor. The data you produce drives a live "
    "internal dashboard the estimators use to decide which subs to chase, "
    "which scopes are covered, and which are still uncovered. Wrong "
    "classifications cause real money to be lost — be conservative, follow "
    "every rule below, and never guess to fill a slot.\n"
    "\n"
    "OUTPUT — strict JSON only. No prose before or after. Schema:\n"
    '{\n'
    '  \"scope_summary\": \"1–3 sentences on what is being bid and where '
    'coverage stands\",\n'
    '  \"vendors\": [\n'
    '    {\n'
    '      \"thread_id\": \"<unchanged from input>\",\n'
    '      \"is_vendor_quote\": true | false,\n'
    '      \"scopes\": [\"<canonical scope>\", ...],          // 0+ items, '
    'see SCOPE TAXONOMY below. Empty array only if is_vendor_quote=false.\n'
    '      \"response_status\": \"<one of the canonical statuses>\",\n'
    '      \"notes\": \"<=140 chars summarizing what happened in this thread\"\n'
    '    }\n'
    '  ]\n'
    '}\n'
    "\n"
    "════════════════════════════════════════════════════\n"
    " STEP 1 — IS THIS A REAL VENDOR-QUOTE THREAD?\n"
    "════════════════════════════════════════════════════\n"
    "Set is_vendor_quote=FALSE (and scopes=[], response_status='Unclear') for:\n"
    "  • Addenda / RFI / bid-date-reminder postings from the GC or planroom.\n"
    "  • Bid invitations FROM the GC to Fusion (we are the recipient, not the requester).\n"
    "  • Pre-bid sign-in sheets, pre-bid meeting agendas, prevailing-wage notices.\n"
    "  • Plan-room access notices (BuildingConnected, OnlinePlanService, Dropbox).\n"
    "  • Internal Fusion-only correspondence (between @fusionelectric-inc.com addresses with no vendor in scope).\n"
    "  • Out-of-office bounces / undeliverables / mailing-list digests.\n"
    "Set is_vendor_quote=TRUE only when:\n"
    "  • Fusion explicitly REQUESTED pricing from a vendor for this project, OR\n"
    "  • A vendor sent us pricing / a proposal / clarifying questions about pricing for this project.\n"
    "\n"
    "════════════════════════════════════════════════════\n"
    " STEP 2 — SCOPE TAXONOMY (multi-select)\n"
    "════════════════════════════════════════════════════\n"
    "scopes is an ARRAY because a single vendor often covers multiple scopes "
    "in one quote (e.g. NCVD does LV+AV; Pavion does FA+SEC; integrators "
    "frequently combine SCADA controls with low-voltage cabling). Pick "
    "EVERY scope the vendor's email indicates they are bidding for. Order "
    "doesn't matter. Use these canonical strings exactly:\n"
    "\n"
    "  'fire alarm' (FA)\n"
    "    Fire-alarm systems, smoke/CO detection, mass notification.\n"
    "    Vendor markers: JCI / Johnson Controls (when in fire context), "
    "Pyro-Comm, Simplex, Siemens Fire, Notifier, Edwards / EST, Konnex, "
    "Cosco, Bay Alarm, Pavion (often FA+SEC).\n"
    "    Subject hints: '- FA', '- FIRE ALARM'.\n"
    "    Body cues: NAC, Notification Appliance Circuits, smoke head count, "
    "addressable panel, mass-notification, parts-and-smarts, Tier 2/Tier 3 "
    "service, prox-lock release. NOT integration/SCADA.\n"
    "\n"
    "  'low voltage' (LV)\n"
    "    Structured cabling, data, voice, telecom, fiber, copper.\n"
    "    Vendor markers: SASCO, NCVD, PCD (low-voltage division), Anixter, "
    "Graybar (data side), Sehi-Riedel, ADI Global.\n"
    "    Subject hints: '- LV', '- LOW VOLTAGE'.\n"
    "    Body cues: Cat6/Cat6A, Systimax, Commscope, Panduit, fiber count, "
    "racks, patch panels, Div 27 10 00, structured cabling.\n"
    "\n"
    "  'lighting' (LTNG)\n"
    "    Light fixtures, lighting controls, dimming.\n"
    "    Vendor markers: CED, Edges, Graybar (lighting side), Acuity, "
    "Lithonia, Lutron, Cooper, RAB, Hubbell lighting, Visual Comfort, "
    "Quoizel.\n"
    "    Subject hints: '- LTNG', '- LIGHTING', '- LIGHT'.\n"
    "    Body cues: fixture schedule, type 'A1', track run lengths, dimming "
    "loads, color temperature, IES files, Title 24 compliance.\n"
    "\n"
    "  'distribution' (DIST)\n"
    "    Switchgear, panelboards, transformers, generators, gear.\n"
    "    Vendor markers: Square D / Schneider, Eaton, ABB, Siemens (gear "
    "side), Cummins (generators), Caterpillar / CAT (generators), Generac, "
    "Kohler, Royal Industries, Loadcenter wholesalers.\n"
    "    Subject hints: '- DIST', '- DISTRIBUTION', '- GEAR', '- SWITCHGEAR', "
    "'- GENERATOR'.\n"
    "    Body cues: 480V / 277/480, 4000A bus, MCC, lugs, breakers (KAIC "
    "rating), ATS, transfer switches, paralleling gear, gen rental.\n"
    "\n"
    "  'trenching' (TRNCH)\n"
    "    Underground civil work that the electrical sub farms out.\n"
    "    Vendor markers: civil trades, dirt subs, Pacific Boring, McGuire "
    "& Hester, ARB.\n"
    "    Subject hints: '- TRNCH', '- TRENCH'.\n"
    "    Body cues: duct bank, conduit pull section, asphalt cut/patch, "
    "saw-cut, bore, jack-and-bore, encasement.\n"
    "\n"
    "  'audio visual' (AV)\n"
    "    A/V, paging, intercom, displays, speakers, video walls.\n"
    "    Vendor markers: Crestron, Biamp, Extron, Polycom, Cisco AV, "
    "Bose Pro, NCVD (AV side), Whitlock / AVI-SPL, Diversified.\n"
    "    Subject hints: '- AV', '- A/V'.\n"
    "    Body cues: DSP, PA system, ceiling speakers, PTZ camera (when "
    "video-conferencing), digital signage, AVoIP, NDI.\n"
    "\n"
    "  'security' (SEC)\n"
    "    Access control, intrusion, surveillance cameras, intercom-as-entry.\n"
    "    Vendor markers: Verkada, Lenel, Avigilon, Genetec, Honeywell "
    "Security, Bosch Security, Pavion (SEC side), Netronix Integration, "
    "ADT Commercial.\n"
    "    Subject hints: '- SEC', '- SECURITY'.\n"
    "    Body cues: card readers, prox cards, Mercury panels, IP cameras, "
    "VMS, NVR, motion detectors, badge printers, panic buttons.\n"
    "\n"
    "  'nurse call' (NC)\n"
    "    Hospital nurse-call / patient-station systems.\n"
    "    Vendor markers: Rauland, Hill-Rom, Responder, Tek-Tone, Cornell, "
    "Stanley Healthcare.\n"
    "    Subject hints: '- NC', '- NURSE CALL'.\n"
    "    Body cues: dome lights, code-blue stations, duty stations, "
    "patient pendants, RTLS.\n"
    "\n"
    "  'integration' (AIC) — automation / integration / controls\n"
    "    SCADA, PLC, BMS/EMS, water-utility pump-station controls, custom "
    "system integration. Use this when the vendor is delivering a control "
    "system rather than a fixed product line. Pump stations, lift stations, "
    "wastewater plants, water-treatment plants almost always need this.\n"
    "    Vendor markers: integrators (Telstar Instruments, Vector "
    "Controls, Inland Pacific, Tesco Controls, Western Allied), PLC OEMs "
    "(Allen-Bradley / Rockwell, Schneider Modicon, Siemens S7), HMI/SCADA "
    "(Wonderware, iFix, Ignition, FactoryTalk).\n"
    "    Subject hints: '- INT', '- INTEGRATION', '- AIC', '- SCADA', "
    "'- CONTROLS'.\n"
    "    Body cues: PLC, RTU, SCADA, HMI, telemetry, flow meters, level "
    "transmitters, Modbus, Profinet, EtherNet/IP, alarm-dialer.\n"
    "    NOTE: lighting controls (Lutron, etc.) are still 'lighting', not "
    "'integration'. BMS for HVAC alone is the mech sub's scope, not ours.\n"
    "\n"
    "  'general' — DO NOT USE unless the vendor is genuinely scope-agnostic "
    "(e.g. distributor sending a multi-trade catalog). 99% of threads have "
    "a real scope. If you are about to emit 'general', re-read the email "
    "and find the right scope or the right combination of scopes.\n"
    "\n"
    "════════════════════════════════════════════════════\n"
    " STEP 3 — SCOPE PRECEDENCE RULES (when signals conflict)\n"
    "════════════════════════════════════════════════════\n"
    " 1. If the SUBJECT contains an explicit scope suffix (e.g. ' - FA', "
    "' - LV/SEC', ' - AV') the scopes array must include EVERY scope the "
    "suffix names. The subject is set by Fusion's estimator and is the "
    "highest-trust signal — never override it from email body content.\n"
    " 2. If the vendor's email body explicitly enumerates multiple scopes "
    "they are quoting on (e.g. 'happy to bid FA, SEC, and AV for this '), "
    "include all of them — even if the subject only listed one.\n"
    " 3. If the vendor company is a known multi-scope integrator/wholesaler "
    "(NCVD, Pavion, Netronix, SASCO, etc.) AND the body discusses both "
    "scopes, output both.\n"
    " 4. Do NOT add scopes purely from a vendor's reputation — must be "
    "supported by either the subject suffix or the body content of THIS "
    "thread.\n"
    " 5. Stand-alone keywords like 'security/camera' inside a thread that "
    "is otherwise about FA do NOT necessarily make it security — re-read "
    "the surrounding sentence. If the email is from a Pavion/Netronix-style "
    "integrator and the thread sibling threads on the same project are "
    "tagged FA, the conservative move is FA (or FA+SEC if the vendor "
    "explicitly says they're quoting both).\n"
    "\n"
    "════════════════════════════════════════════════════\n"
    " STEP 4 — RESPONSE STATUS (single value)\n"
    "════════════════════════════════════════════════════\n"
    "Use EXACTLY one. The estimators rely on this column to decide who to "
    "chase next — a soft 'I'll get back to you' must NOT be flagged as if "
    "we have pricing.\n"
    "\n"
    "  'Pending'\n"
    "      Quote was requested by Fusion. The vendor has not replied at all "
    "in this thread — no acknowledgement, no questions, no decline.\n"
    "\n"
    "  'Acknowledged'\n"
    "      Vendor replied confirming they received the request and intend "
    "to bid (or are 'looking into it', 'will send pricing later this week', "
    "'reviewing the docs', 'pricing forthcoming'). NO actual numbers, NO "
    "attached proposal yet. This is the default for any soft response — "
    "lean here whenever you're unsure between Acknowledged and Received.\n"
    "\n"
    "  'Asked Questions'\n"
    "      Vendor sent clarifying questions (scope, schedule, drawings, "
    "addendum impact, sub-vendor swaps) and we have not yet given them "
    "everything they need to price. Stays 'Asked Questions' until they "
    "either go silent (still Asked Questions), reply with pricing "
    "(promote to Received), or decline.\n"
    "\n"
    "  'Declined'\n"
    "      Vendor said no-bid / not bidding / passing on this one. Includes "
    "polite declines and 'we don't service this area / not a fit'.\n"
    "\n"
    "  'Received'\n"
    "      STRICT bar. Use ONLY when the vendor delivered an actionable "
    "quote in THIS thread:\n"
    "        • A dollar figure or itemized pricing breakdown in the body, OR\n"
    "        • An attached PDF / Excel / signed proposal containing pricing, OR\n"
    "        • An explicit lump-sum / NTE figure with terms.\n"
    "      The following do NOT qualify as Received:\n"
    "        • 'Pricing forthcoming', 'I'll get you pricing soon', 'sending "
    "shortly', 'working on it', 'budgetary number incoming'.\n"
    "        • Replies that mention 'pricing' or 'quote' as a noun without "
    "actually delivering numbers.\n"
    "        • Attachments that are scope sheets / cut sheets without prices.\n"
    "      When in doubt → 'Acknowledged'. The estimator would rather chase "
    "a vendor unnecessarily than miss a missing quote.\n"
    "\n"
    "  'Unclear'\n"
    "      Thread is genuinely ambiguous and none of the above fit. Use "
    "sparingly — most threads can be classified.\n"
    "\n"
    "════════════════════════════════════════════════════\n"
    " STEP 5 — NOTES (≤140 chars)\n"
    "════════════════════════════════════════════════════\n"
    "Single sentence summarizing what changed in this thread. Lead with the "
    "vendor's company + key fact. Examples:\n"
    "  • 'Pavion (Steven Lewis) confirmed bidding FA + SEC, parts-and-smarts proposal coming next week.'\n"
    "  • 'CED (Keith Young) asked for track run lengths; Gabe replied with addendum #1 counts. No pricing yet.'\n"
    "  • 'JCI (Silvestre Cruz) sent FA parts-and-smarts quote 4/28; addendum #1 confirmed no impact.'\n"
    "Do NOT include scope abbreviations the user already sees on the chip. "
    "Do NOT include the date if it's already on the row.\n"
    "\n"
    "════════════════════════════════════════════════════\n"
    " STEP 6 — scope_summary (project-level, 1–3 sentences)\n"
    "════════════════════════════════════════════════════\n"
    "Plain-English description of what's being bid + where coverage stands. "
    "Examples:\n"
    "  • 'Bidwell Park Hayward — FA upgrade to existing Simplex panel. "
    "Pavion confirmed FA bid, JCI parts/smarts in. SEC scope still uncovered.'\n"
    "  • 'San Juan Pump Station rehab — needs DIST, INTEGRATION (PLC/SCADA "
    "controls), and TRNCH coverage. Telstar bid pending; no DIST quotes yet.'\n"
    "If a project clearly involves a pump station, lift station, water/"
    "wastewater plant, mention 'integration' or 'SCADA' explicitly so the "
    "estimator notices the AIC scope is in play.\n"
    "\n"
    "════════════════════════════════════════════════════\n"
    " ANTI-PATTERNS — do not\n"
    "════════════════════════════════════════════════════\n"
    "  ✗ Don't tag scope based on the vendor's company name alone — must "
    "be supported by the email body or subject.\n"
    "  ✗ Don't use 'Received' as a hopeful classification. Numbers or no.\n"
    "  ✗ Don't use 'general' to dodge a hard call. Find the right scope(s) "
    "or admit Unclear.\n"
    "  ✗ Don't include addenda/notice threads (is_vendor_quote=false).\n"
    "  ✗ Don't echo the project name back into notes.\n"
    "\n"
    "Return STRICT JSON ONLY — no markdown fences, no prose."
)


def get_anthropic_client():
    if anthropic is None:
        return None
    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    # max_retries=5 + the SDK's built-in exponential backoff lets us survive
    # transient 429s without each project's call dying outright.
    return anthropic.Anthropic(api_key=api_key, max_retries=5)


def _throttle_after_call(input_tokens, last_input_total):
    """Sleep so we don't blow past Tier 1's 30K input-tokens-per-minute cap.

    Strategy: keep a rolling estimate of the last 60s of input tokens;
    sleep before the next call if the next ~30K window would overflow.
    Conservative — pads by 20% to leave headroom for shared org workloads.
    """
    import time
    if input_tokens > 25000:
        # That one call already came close to the per-minute cap. Wait
        # the better part of a minute before the next call to drain it.
        time.sleep(55)
    elif input_tokens > 10000:
        time.sleep(20)
    else:
        time.sleep(3)


def thread_signature(thread):
    """Hash all message IDs + their internalDates so we can detect when
    a thread has new activity vs. just being re-fetched."""
    parts = []
    for m in thread.get("messages", []) or []:
        parts.append(f"{m.get('id','')}:{m.get('internalDate','')}")
    return "|".join(parts)


# Bump this when the LLM_SYSTEM_PROMPT changes in a way that should force a
# full re-classification (e.g. tightening the response-status definitions).
# The prompt version is folded into project_signature() so existing cache
# entries miss and Claude is re-asked.
PROMPT_VERSION = "v4-multi-scope-integration-2026-05-01"


def project_signature(threads):
    """Combined signature over all threads in a project. Includes the
    PROMPT_VERSION so changes to the system prompt bust the cache."""
    sigs = sorted(thread_signature(t) for t in threads)
    return PROMPT_VERSION + "::" + "::".join(sigs)


def _cloud_kv_url():
    """If running in cloud mode (SUPABASE_SERVICE_KEY set), return the
    PostgREST URL for the single-row gmail_kv_cloud table where the LLM
    cache lives keyed by 'gmail_llm_cache'. Returns None on PC mode."""
    if not os.environ.get("SUPABASE_SERVICE_KEY"):
        return None
    return "https://dltuvsdwrujjsmiotaxy.supabase.co/rest/v1/gmail_kv_cloud"


def load_llm_cache():
    cloud_url = _cloud_kv_url()
    if cloud_url:
        # Cloud: read the single-row blob from gmail_kv_cloud.
        import urllib.request as _ur
        import urllib.error as _ue
        key = os.environ["SUPABASE_SERVICE_KEY"]
        try:
            req = _ur.Request(
                cloud_url + "?key=eq.gmail_llm_cache&select=value",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
            )
            with _ur.urlopen(req, timeout=20) as resp:
                rows = json.loads(resp.read().decode("utf-8") or "[]")
                if rows and isinstance(rows[0].get("value"), dict):
                    return rows[0]["value"]
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] cloud cache read failed: {exc}", file=sys.stderr)
        return {}
    # PC mode -- local JSON file.
    if not LLM_CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(LLM_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_llm_cache(cache):
    cloud_url = _cloud_kv_url()
    if cloud_url:
        import urllib.request as _ur
        import urllib.error as _ue
        key = os.environ["SUPABASE_SERVICE_KEY"]
        body = json.dumps([{
            "key": "gmail_llm_cache",
            "value": cache,
            "updated_at": dt.datetime.utcnow().isoformat() + "Z",
        }]).encode("utf-8")
        try:
            req = _ur.Request(
                cloud_url,
                data=body,
                method="POST",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
            with _ur.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201, 204):
                    print(f"[warn] cloud cache write returned HTTP {resp.status}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] cloud cache write failed: {exc}", file=sys.stderr)
        return
    # PC mode.
    LLM_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ---- Credit / billing alert ---------------------------------------------

def is_credit_or_billing_error(exc):
    """Return True if the exception looks like an Anthropic credit-balance
    or billing wall (vs. a transient rate-limit or network blip).

    The SDK raises BadRequestError (400) with type 'invalid_request_error'
    and a message containing 'credit balance is too low' when the account
    runs out of money. We also catch the broader 'billing' / 'payment'
    phrases so a future wording change still trips the alert.
    """
    if exc is None:
        return False
    text = (str(exc) or "").lower()
    needles = (
        "credit balance is too low",
        "credit balance",
        "insufficient credits",
        "payment required",
        "billing",
        "your plan does not have access",
    )
    return any(n in text for n in needles)


def _load_credit_alert_state():
    if not CREDIT_ALERT_STATE_PATH.is_file():
        return {}
    try:
        return json.loads(CREDIT_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_credit_alert_state(state):
    try:
        CREDIT_ALERT_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def send_credit_alert(service, error_message, project_label=None):
    """Email alex@ with a top-up link when Claude returns a credit-out error.

    Cooldown enforced via CREDIT_ALERT_STATE_PATH so the 15-minute auto
    task doesn't spam the inbox once the wall is hit.
    """
    state = _load_credit_alert_state()
    last_iso = state.get("last_sent")
    if last_iso:
        try:
            last = dt.datetime.fromisoformat(last_iso)
            if (dt.datetime.utcnow() - last).total_seconds() < CREDIT_ALERT_COOLDOWN_HOURS * 3600:
                return False
        except Exception:  # noqa: BLE001
            pass

    try:
        import base64
        from email.mime.text import MIMEText

        subject = "Fusion Portal: Claude AI credit balance hit — top up"
        body_lines = [
            "The Fusion Portal auto-update task tried to call the Claude API",
            "and got a credit / billing error. AI parsing is paused until the",
            "balance is topped up.",
            "",
            f"Top up here: https://console.anthropic.com/settings/billing",
            "",
            "Error detail:",
            f"  {error_message}",
        ]
        if project_label:
            body_lines.insert(2, f"Project being processed when it hit: {project_label}")
        body_lines.append("")
        body_lines.append(f"Sent {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} from parse-gmail-quotes.py")
        body_lines.append(f"Cooldown: another alert won't fire for {CREDIT_ALERT_COOLDOWN_HOURS}h.")

        msg = MIMEText("\n".join(body_lines))
        msg["To"] = CREDIT_ALERT_TO
        msg["From"] = CREDIT_ALERT_TO
        msg["Subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()

        state["last_sent"] = dt.datetime.utcnow().isoformat()
        state["last_error"] = error_message
        _save_credit_alert_state(state)
        print(f"[credit-alert] Sent top-up email to {CREDIT_ALERT_TO}", file=sys.stderr)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[credit-alert] Failed to send alert: {exc}", file=sys.stderr)
        return False


def truncate(s, n):
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "…"


def thread_summary_for_llm(thread):
    """Compact representation of a thread for the LLM — subject + each
    message's role, sender, and a body snippet."""
    msgs = sorted(
        thread.get("messages", []) or [],
        key=lambda m: parse_internal_date(m) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
    )
    if not msgs:
        return None
    first_subject = header(msgs[0], "Subject")
    rendered = []
    for m in msgs:
        sender = header(m, "From")
        addr = parse_sender(sender)[1]
        role = "US" if is_outbound(addr) else "VENDOR"
        date = parse_internal_date(m)
        date_str = date.date().isoformat() if date else "?"
        snippet = truncate(message_body_text(m.get("payload")), 600).strip().replace("\n", " ")
        rendered.append(f"[{date_str} {role}] {sender}: {snippet}")
    return {
        "thread_id": thread.get("id"),
        "subject": first_subject,
        "messages": rendered,
    }


def enrich_with_claude(client, project_label, project_number, threads):
    """One Claude call per project. Returns dict keyed by thread_id with
    enrichment fields, plus a project-level scope_summary."""
    bundles = [s for s in (thread_summary_for_llm(t) for t in threads) if s]
    if not bundles:
        return {"scope_summary": "", "by_thread": {}}

    user_msg = json.dumps({
        "project_label": project_label,
        "project_number": project_number,
        "threads": bundles,
    }, ensure_ascii=False)

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": LLM_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = ""
    for block in (resp.content or []):
        if getattr(block, "type", "") == "text":
            text += block.text

    # Parse JSON. The model occasionally wraps in ```json blocks — strip those.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] Claude returned non-JSON for {project_label}: {exc}", file=sys.stderr)
        return {"scope_summary": "", "by_thread": {}, "error": str(exc), "raw": cleaned[:200]}

    by_thread = {}
    # Canonical scope set the prompt is allowed to emit.
    CANONICAL_SCOPES = {
        "fire alarm", "low voltage", "lighting", "distribution",
        "trenching", "audio visual", "security", "nurse call",
        "integration", "general",
    }

    def _canon_scope_list(raw):
        """Normalize Claude's output into a deduped list of canonical scopes.
        Accepts the new 'scopes' array OR the legacy 'scope' string for
        forward/backward compatibility. Drops 'general' if any other scope
        is present (general is a fallback, not a co-tag)."""
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str):
            items = [raw]
        seen = []
        for s in items:
            if not isinstance(s, str):
                continue
            v = s.strip().lower()
            if v in CANONICAL_SCOPES and v not in seen:
                seen.append(v)
        if len(seen) > 1 and "general" in seen:
            seen.remove("general")
        return seen

    for v in parsed.get("vendors", []) or []:
        tid = v.get("thread_id")
        if not tid:
            continue
        # Prefer the new 'scopes' array; fall back to the legacy 'scope' string.
        scopes = _canon_scope_list(v.get("scopes")) if v.get("scopes") is not None else _canon_scope_list(v.get("scope"))
        primary = scopes[0] if scopes else ""
        by_thread[tid] = {
            "scope": primary,            # legacy single value
            "scopes": scopes,            # new multi-scope array
            "response_status": v.get("response_status") or "",
            "notes": v.get("notes") or "",
            # Threads marked false here get dropped from the vendor list
            # (admin notices, addenda postings, bid reminders).
            "is_vendor_quote": v.get("is_vendor_quote", True),
        }

    usage = resp.usage
    return {
        "scope_summary": parsed.get("scope_summary") or "",
        "by_thread": by_thread,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        },
    }


# ---- Output --------------------------------------------------------------

HTML_WRAPPER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Current Bids Vendor Tracker (auto)</title>
</head>
<body>
<p>Headless parse from Gmail. JSON payload below is consumed by Convert-QuoteTracker.ps1.</p>
<script id="cb-data" type="application/json">__JSON__</script>
</body>
</html>
"""


def emit_html(payload, out_path):
    blob = json.dumps(payload, indent=2)
    html = HTML_WRAPPER.replace("__JSON__", htmlmod.escape(blob, quote=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


# ---- Main ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="Path to Current Bids Tracker.html")
    parser.add_argument("--user-email", default="alex@" + USER_DOMAIN)
    parser.add_argument("--skip-llm", action="store_true", help="Skip Claude enrichment even if API key is set (rule-based only).")
    args = parser.parse_args()

    out_path = Path(args.out)
    today = dt.date.today()

    service = get_service()

    bid_due_dates = load_bids_due_dates()
    print(f"Loaded {len(bid_due_dates)} bid due dates from portal data.")

    # Optional Claude enrichment — gate on API key + library + flag.
    claude = None if args.skip_llm else get_anthropic_client()
    if claude:
        print(f"Claude enrichment enabled — model: {CLAUDE_MODEL}")
        llm_cache = load_llm_cache()
    else:
        if args.skip_llm:
            print("Skipping Claude enrichment (--skip-llm).")
        elif anthropic is None:
            print("Claude enrichment unavailable: anthropic SDK not installed (pip install anthropic).")
        else:
            print("Claude enrichment unavailable: no CLAUDE_API_KEY / ANTHROPIC_API_KEY in env.")
        llm_cache = {}

    project_labels = list_project_labels(service)
    print(f"Found {len(project_labels)} project labels under '{ROOT_LABEL}'.")

    projects = []
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    cache_hits = 0
    llm_calls = 0

    for label_id, label_name in project_labels:
        thread_ids = list_thread_ids_for_label(service, label_id)
        proj_num, proj_name = project_meta_from_label(label_name)

        # Fetch all threads up-front so we can compute the project signature
        # before deciding whether to call Claude.
        fetched_threads = []
        skipped = []
        for tid in thread_ids:
            try:
                fetched_threads.append(fetch_thread(service, tid))
            except Exception as exc:  # noqa: BLE001
                skipped.append({"thread_id": tid, "subject": "", "reason": f"fetch failed: {exc}"})

        vendor_rows = []
        thread_by_id = {t.get("id"): t for t in fetched_threads}
        for thread in fetched_threads:
            row, reason = parse_thread(thread, today, bid_due_dates, proj_num)
            if row is None:
                first = (thread.get("messages") or [{}])[0]
                skipped.append({
                    "thread_id": thread.get("id"),
                    "subject": header(first, "Subject"),
                    "reason": reason or "skipped",
                })
            else:
                vendor_rows.append(row)

        # Layer LLM enrichment on top — only re-call Claude if the project's
        # combined thread signature has changed since the last cycle.
        scope_summary = ""
        if claude:
            sig = project_signature(fetched_threads)
            cached = llm_cache.get(label_name)
            if cached and cached.get("signature") == sig:
                scope_summary = cached.get("scope_summary", "")
                by_thread = cached.get("by_thread", {}) or {}
                cache_hits += 1
                print(f"  · {label_name}: cached LLM enrichment")
            else:
                try:
                    enrichment = enrich_with_claude(claude, label_name, proj_num, fetched_threads)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [warn] Claude call failed for {label_name}: {exc}", file=sys.stderr)
                    enrichment = {"scope_summary": "", "by_thread": {}}
                    if is_credit_or_billing_error(exc):
                        send_credit_alert(service, str(exc), project_label=label_name)
                        # No point hammering the API for the rest of the run
                        # once the account is walled — bail out of the LLM
                        # loop and let the rule-based output ship.
                        claude = None
                scope_summary = enrichment.get("scope_summary", "")
                by_thread = enrichment.get("by_thread", {}) or {}
                usage = enrichment.get("usage") or {}
                in_toks = usage.get("input_tokens", 0)
                total_input += in_toks
                total_output += usage.get("output_tokens", 0)
                total_cache_read += usage.get("cache_read_input_tokens", 0)
                total_cache_create += usage.get("cache_creation_input_tokens", 0)
                # Persist the cache after EVERY successful call so a
                # subsequent rate-limit failure doesn't lose progress.
                llm_cache[label_name] = {
                    "signature": sig,
                    "scope_summary": scope_summary,
                    "by_thread": by_thread,
                }
                save_llm_cache(llm_cache)
                llm_calls += 1
                print(f"  · {label_name}: Claude enriched ({in_toks} in / {usage.get('output_tokens',0)} out)")
                # Throttle to stay under the Tier 1 30K input-TPM cap.
                _throttle_after_call(in_toks, None)
            # Scope priority (deliberate, in order):
            #   1. Subject-line suffix hint (e.g. ' - FA', ' - SEC') —
            #      these are human-set by the estimator and never wrong.
            #      Claude can't be allowed to override these because LLMs
            #      sometimes follow vendor-company priors over the actual
            #      subject (e.g. "JCI = fire alarm" reflex even when the
            #      subject literally says SEC).
            #   2. Claude's classification (when no subject hint).
            #   3. Loose subject substring match.
            #   4. 'general'.
            # Also: drop admin/notice threads (Claude flagged is_vendor_quote=false).
            cleaned_rows = []
            subj_scopes_for = {}  # thread_id -> [subject-suffix scopes]
            for v in vendor_rows:
                tid = v.get("request_thread_id")
                enr = by_thread.get(tid) or {}
                if enr.get("is_vendor_quote") is False:
                    continue  # admin/notice — drop from vendor list
                subj_list = fallback_scopes_from_subject(v.get("request_subject"))
                subj_scopes_for[tid] = subj_list
                claude_scopes = enr.get("scopes") or ([enr.get("scope")] if enr.get("scope") else [])
                claude_scopes = [s.strip().lower() for s in claude_scopes if isinstance(s, str) and s.strip()]
                # Final scopes array — subject suffix wins (always), but if Claude
                # added EXTRA scopes the subject didn't mention, keep them so the
                # vendor's full coverage shows on the chip row. Drops 'general' if
                # any other scope is present.
                if subj_list:
                    final_scopes = list(subj_list)
                    for cs in claude_scopes:
                        if cs and cs != "general" and cs not in final_scopes:
                            final_scopes.append(cs)
                else:
                    final_scopes = list(claude_scopes)
                if len(final_scopes) > 1 and "general" in final_scopes:
                    final_scopes = [s for s in final_scopes if s != "general"]
                v["scopes"] = final_scopes
                v["scope"] = final_scopes[0] if final_scopes else (claude_scopes[0] if claude_scopes else "")
                if enr.get("response_status"):
                    v["response_status"] = enr["response_status"]
                if enr.get("notes"):
                    v["notes"] = enr["notes"]
                cleaned_rows.append(v)
            vendor_rows = cleaned_rows

            # Per-project vendor-domain consistency pass.
            # Some projects have multiple staff members from the same vendor
            # (e.g. Pavion's slewis@pavion.com tagged FA twice and Pavion-
            # affiliated ktchang@netronixint.com tagged SEC once because
            # one stray email body mentioned 'security/camera scope files').
            # When several threads share a vendor domain on the same project,
            # use the dominant non-general scope so the vendor's icon stops
            # flapping based on per-message wording. Threads with an explicit
            # subject-suffix hint are immune — those are the "trust the icons"
            # signals the user told us never to override.
            #
            # Cross-domain aliases (e.g. Pavion ↔ Netronix Integration are
            # one parent company on different domains) live in
            # scripts/vendor-aliases.json and let the grouping logic merge
            # those domains into one canonical bucket.
            from collections import Counter as _C
            try:
                _aliases_path = SCRIPTS_DIR / "vendor-aliases.json"
                _alias_raw = json.loads(_aliases_path.read_text(encoding="utf-8")) if _aliases_path.is_file() else {}
            except Exception:  # noqa: BLE001
                _alias_raw = {}
            # Flatten: domain -> canonical key
            domain_to_canonical = {}
            for canonical, domains in _alias_raw.items():
                if canonical.startswith("_") or not isinstance(domains, list):
                    continue
                for d in domains:
                    if isinstance(d, str) and d.strip():
                        domain_to_canonical[d.strip().lower()] = canonical

            def _domain_key(email):
                if "@" not in email:
                    return None
                domain = email.split("@", 1)[1].lower()
                # Use the alias-mapped canonical key if available.
                if domain in domain_to_canonical:
                    return domain_to_canonical[domain]
                # Otherwise fall back to base eTLD+1 (drop sub-domain noise).
                return ".".join(domain.split(".")[-2:]) if domain.count(".") > 1 else domain

            domain_groups = {}
            for v in vendor_rows:
                email = (v.get("vendor_contact_email") or "").lower().strip()
                base = _domain_key(email)
                if not base:
                    continue
                domain_groups.setdefault(base, []).append(v)
            for base, rows in domain_groups.items():
                if len(rows) < 2:
                    continue
                # Tally scope frequency across the group's threads, counting
                # multi-scope entries individually so a vendor that does
                # FA+SEC contributes to both buckets. 'general' is ignored
                # because it's a fallback, not real coverage.
                tally = _C()
                for r in rows:
                    arr = r.get("scopes") or ([r.get("scope")] if r.get("scope") else [])
                    for s in arr:
                        sc = (s or "").strip().lower()
                        if sc and sc != "general":
                            tally[sc] += 1
                if not tally:
                    continue
                counts = tally.most_common()
                dominant, dominant_n = counts[0]
                # Skip ties — equal counts mean no consensus and we shouldn't pick.
                if len(counts) > 1 and counts[1][1] == dominant_n:
                    continue
                for r in rows:
                    tid = r.get("request_thread_id")
                    if subj_scopes_for.get(tid):
                        continue  # explicit subject suffix wins, never override
                    cur_arr = r.get("scopes") or ([r.get("scope")] if r.get("scope") else [])
                    cur_arr = [(s or "").strip().lower() for s in cur_arr if s]
                    if dominant in cur_arr:
                        continue
                    # Per-vendor consistency: when a vendor (Pavion-Netronix-RFI
                    # being the canonical example) has a clear dominant scope
                    # across all their threads on this project, REPLACE the
                    # per-thread scope with that dominant. Per-thread wording
                    # ("security/camera scope files for rebid") often describes
                    # logistics rather than the vendor's actual bid scope —
                    # the user explicitly asked us to trust the vendor's
                    # project role over per-message keywords. The thread's
                    # specific content stays preserved in the notes.
                    prev_scope = ",".join(cur_arr) or "(none)"
                    r["scopes"] = [dominant]
                    r["scope"] = dominant
                    existing = (r.get("notes") or "").strip()
                    marker = f"[scope set from {base} dominant: {dominant}, per-thread guess was {prev_scope}]"
                    r["notes"] = (existing + "  " + marker).strip() if existing else marker
        else:
            print(f"  · {label_name}: {len(vendor_rows)} vendors, {len(skipped)} skipped")

        bid_due = bid_due_dates.get(proj_num)
        projects.append({
            "project_label": label_name,
            "project_number": proj_num,
            "project_name": proj_name,
            "scope_summary": scope_summary,
            "bid_due_date": bid_due,
            "thread_count": len(thread_ids),
            "vendors": vendor_rows,
            "skipped_threads": skipped,
        })

    if claude:
        save_llm_cache(llm_cache)
        # Cost estimate (rough, Opus 4.7: $5 in / $25 out per 1M; cache reads ~10% of input)
        est_cost = (
            (total_input - total_cache_read) * 5e-6
            + total_cache_read * 0.5e-6
            + total_cache_create * 6.25e-6
            + total_output * 25e-6
        )
        print(
            f"\nLLM summary: {llm_calls} calls / {cache_hits} cached hits · "
            f"{total_input} input / {total_output} output / "
            f"{total_cache_read} cache-read / {total_cache_create} cache-create tokens · "
            f"est. ${est_cost:.4f}"
        )

    # generated_at uses local time so the dashboard timestamp matches what
    # the user expects in their timezone.
    now = dt.datetime.now()
    if claude:
        if llm_calls == 0 and cache_hits > 0:
            engine = "claude (all cached)"
        elif llm_calls > 0 and cache_hits > 0:
            engine = "claude (mixed)"
        else:
            engine = "claude"
        engine_model = CLAUDE_MODEL
    else:
        engine = "rule-based"
        engine_model = ""

    payload = {
        "generated_at": today.isoformat(),
        "generated_at_iso": now.isoformat(timespec="seconds"),
        "user_email": args.user_email,
        "parse_engine": engine,
        "parse_model": engine_model,
        "projects": projects,
    }

    emit_html(payload, out_path)
    print(f"Wrote {out_path} ({sum(len(p['vendors']) for p in projects)} vendor rows across {len(projects)} projects).")

    # Cloud mode: also persist the parsed payload to a single-row
    # Supabase table the Bay Bid List page can read directly. Replaces
    # the static-JS-deploy chain.
    if os.environ.get("SUPABASE_SERVICE_KEY"):
        try:
            import urllib.request as _ur
            key = os.environ["SUPABASE_SERVICE_KEY"]
            url = "https://dltuvsdwrujjsmiotaxy.supabase.co/rest/v1/quote_tracker_cloud"
            body = json.dumps([{
                "id": "current",
                "payload": payload,
                "generated_at": payload.get("generated_at_iso") or dt.datetime.utcnow().isoformat() + "Z",
                "updated_at": dt.datetime.utcnow().isoformat() + "Z",
            }]).encode("utf-8")
            req = _ur.Request(
                url, data=body, method="POST",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
            with _ur.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 201, 204):
                    print("Wrote quote_tracker_cloud (current snapshot) to Supabase.")
                else:
                    print(f"[warn] quote_tracker_cloud write returned HTTP {resp.status}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] quote_tracker_cloud write failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
