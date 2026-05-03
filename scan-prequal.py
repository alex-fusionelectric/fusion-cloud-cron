"""scan-prequal.py — scans Dropbox bid folders for prequalification language.

For every `EST# {est_number} ...` folder under the Dropbox estimating root,
walks the PLANS & SPECS tree looking for spec documents (Division 00 / 00 21
00 type files). Pulls text out of each PDF via pypdf, searches for prequal
keywords, and emits a JSON map keyed by est_number.

Output shape:
  {
    "26-201": {
      "prequal_required": "yes" | "no" | "unknown",
      "evidence": "...short excerpt that triggered the match...",
      "source_file": "26-201/.../00 21 13 - INSTRUCTIONS TO BIDDERS.pdf",
      "scanned_pdfs": 4,
      "scanned_at": "2026-04-30T..."
    },
    ...
  }

Logic:
  - "yes" if a strong-positive phrase is found ("prequalification REQUIRED",
    "must be prequalified", "DSA prequalification", "DIR PWCR registration",
    "must complete prequalification questionnaire", etc.)
  - "no" if we read enough Division-00 text and find an explicit statement
    like "prequalification is not required" or "no prequalification".
  - "unknown" if we can't find Division-00 / instructions-to-bidders text
    or matches are ambiguous.

Caveats: the keyword scan is deterministic and fast but not perfect. Once
billing is unblocked we can layer Claude on the same evidence text for a
nuanced read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

try:
    import pypdf
except ImportError:
    print("Missing pypdf. Run: python -m pip install pypdf", file=sys.stderr)
    sys.exit(1)

# Windows file-attribute bits. Skipping these avoids triggering Dropbox /
# OneDrive cloud-fetch when we read a file that isn't locally cached.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000  # reparse-point variant Dropbox uses


def is_cloud_only(path):
    """True if reading this file would hit the network. Always False on
    non-Windows; on Windows we read st_file_attributes from os.stat()."""
    try:
        attrs = path.stat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & (FILE_ATTRIBUTE_OFFLINE
                          | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                          | FILE_ATTRIBUTE_RECALL_ON_OPEN))


# Folder names the script considers part of the "front-end / spec" bundle.
SPEC_FOLDER_HINTS = [
    "spec", "specification", "division 00", "div 00", "div00",
    "instructions to bidders", "front end", "00 ",
    "project manual", "bid documents", "front-end", "frontend",
]

# Prefer files whose name mentions any of these — they almost always
# contain the prequal-related language.
SPEC_FILE_HINTS = [
    "instructions to bidders", "division 00", "div 00",
    "00 11", "00 21", "00 22", "00 31",  # CSI MasterFormat refs
    "supplementary instructions", "advertisement", "notice to bidders",
    "bidders qualifications", "bidder qualifications",
    "prequalification", "prequal",
    "project manual", "specifications", "specification book",
]

# Folders to skip entirely — old archives, addenda plan sheets (only the
# spec/front-end carries prequal language, not the addenda drawings).
SKIP_FOLDER_HINTS = [
    "old bid", "old bids", "archive", "archived",
    "addenda-plans", "addenda plans", "drawings only",
    "plan sheets", "bid drawings",
]

# Strong matches → prequal_required = "yes".
POSITIVE_PHRASES = [
    r"prequalif\w+\s+(is\s+)?required",
    r"required\s+to\s+(be\s+)?prequalif",
    r"must\s+(be\s+)?prequalif",
    r"shall\s+(be\s+)?prequalif",
    r"all\s+bidders\s+must\s+prequalif",
    r"only\s+prequalified\s+(contractors|bidders)",
    r"prequalification\s+(application|questionnaire|is\s+mandatory)",
    r"DIR\s+(public\s+works\s+contractor|PWCR)\s+registration\s+(is\s+)?required",
    r"DSA\s+prequalif",
    r"public\s+contract\s+code\s+section\s+20111\.6",  # CA prequal statute
    r"Public\s+Contract\s+Code\s+§?\s*20651\.5",
]

# Strong matches → "no".
NEGATIVE_PHRASES = [
    r"prequalif\w+\s+is\s+not\s+required",
    r"no\s+prequalif",
    r"prequalif\w+\s+(is\s+)?(not\s+)?necessary",
]

# Loose match — bumps the result toward "yes" if combined with weaker signal.
WEAK_POSITIVE = [
    r"prequalif\w+",
]


def find_bid_folders(root, active_only_set=None):
    """Return [(est_number, folder_path), ...].

    If active_only_set is provided (e.g. {"25-396", "26-201", ...}), filter
    to just those est_numbers — skips every old/archived EST# folder so we
    don't scan or cloud-fetch documents for bids we're not actively
    working on.
    """
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        m = re.match(r"^EST#\s*([0-9]{2}-[0-9]{3,4})\b", entry.name, re.IGNORECASE)
        if not m:
            continue
        est = m.group(1).upper()
        if active_only_set is not None and est not in active_only_set:
            continue
        out.append((est, entry))
    return out


ACTIVE_BID_STATUSES = {"bidding", "bid or bail"}


def load_active_est_numbers(bids_json_path):
    """Read fusion-portal's bids-data.json and return the set of est_numbers
    whose status is currently active (Bidding OR Bid Or Bail). Returns None
    if the file can't be read so callers fall back to scanning everything.

    'Bid Or Bail' represents bids the team is still considering — they
    appear in the vendor quote tracker, so they need prequal pills too.
    Sent/Awarded/Not Awarded are filtered out (no pill needed)."""
    if not bids_json_path or not bids_json_path.is_file():
        return None
    try:
        data = json.loads(bids_json_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not parse bids-data.json: {exc}", file=sys.stderr)
        return None
    out = set()
    for b in (data.get("bids") or []):
        status = str(b.get("status") or "").strip().lower()
        if status not in ACTIVE_BID_STATUSES:
            continue
        # Some rows are pure project-list mirrors (sourceList=projectList);
        # those don't represent live bids and shouldn't trigger a scan.
        if b.get("sourceList") == "projectList":
            continue
        est = str(b.get("estNumber") or "").strip().upper()
        if est:
            out.add(est)
    return out


def candidate_specs(bid_folder):
    """Return [(pdf_path, allow_fetch_if_cloud), ...] in priority order.

    The first 2 highest-priority candidates AND any obvious-prequal-named
    files get `allow_fetch=True` — we'll trigger a Dropbox download for
    those even if cloud-only (typically <200KB each, so worth the bandwidth).
    Everything else only gets scanned if already local (cached).

    Skips: files in OLD BID / addenda-plans / drawings-only folders, plus
    any large file that's cloud-only (>5MB) since those are usually plan
    sets, not specs.
    """
    plans_root = bid_folder / "PLANS & SPECS"
    if not plans_root.is_dir():
        plans_root = bid_folder
    p1, p2, p3 = [], [], []
    for pdf in plans_root.rglob("*.pdf"):
        if not pdf.is_file():
            continue
        path_lc = str(pdf.parent).lower()
        if any(skip in path_lc for skip in SKIP_FOLDER_HINTS):
            continue
        # Avoid pulling massive plan-set PDFs even if filename happens to
        # match — anything >5MB cloud-only is almost certainly drawings.
        if is_cloud_only(pdf):
            try:
                size = pdf.stat().st_size
            except OSError:
                size = 0
            if size > 5 * 1024 * 1024:
                continue
        name_lc = pdf.name.lower()
        if any(h in name_lc for h in SPEC_FILE_HINTS):
            p1.append(pdf)
        elif any(h in path_lc for h in SPEC_FOLDER_HINTS):
            p2.append(pdf)
        else:
            p3.append(pdf)
    # Top 2 P1 + top 1 P2 are allowed cloud-fetch. Remaining only scanned
    # if already local. Total worst-case downloads per bid: 3 small PDFs.
    out = []
    out.extend([(p, True) for p in p1[:2]])
    out.extend([(p, True) for p in p2[:1]])
    out.extend([(p, False) for p in p1[2:6]])
    out.extend([(p, False) for p in p2[1:3]])
    out.extend([(p, False) for p in p3[:2]])
    return out


def extract_text(pdf_path, max_pages=40):
    """Pull text from the first N pages of a PDF. We cap to avoid spending
    a minute on a 500-page plan set when prequal lives at the very front."""
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        return "", f"pdf-error: {exc}"
    return _read_reader_pages(reader, max_pages), None


def extract_text_from_bytes(pdf_bytes, max_pages=40):
    """Cloud variant: take raw PDF bytes (e.g. from a Dropbox download) and
    return up-to-N-pages of extracted text. Same return contract as
    extract_text(): (text, err_or_None). Used by cloud-sync-prequal.py."""
    import io
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        return "", f"pdf-error: {exc}"
    return _read_reader_pages(reader, max_pages), None


def _read_reader_pages(reader, max_pages):
    chunks = []
    for p in reader.pages[:max_pages]:
        try:
            chunks.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(chunks)


# Hard-coded prequal rules. The user maintains these directly: certain
# owners' projects always (or never) require prequalification regardless
# of what the spec PDFs say. Keyed by a substring match on the project
# folder name, case-insensitive. First match wins.
HARDCODED_PREQUAL_RULES = [
    # Stanford Health Care projects never require prequal (per Alex).
    # Folder names look like "EST# 26-221 SHC 300P NUC MED PATIO".
    {"match": "SHC", "verdict": "no", "evidence": "Rule: Stanford Health Care (SHC) projects do not require prequalification."},
    {"match": "STANFORD HEALTH", "verdict": "no", "evidence": "Rule: Stanford Health Care projects do not require prequalification."},
]


def hardcoded_prequal_for(folder_name):
    """Return the rule dict whose `match` substring is in folder_name (case-insensitive),
    or None if none match. Shared between local and cloud scanners."""
    upper = (folder_name or "").upper()
    for r in HARDCODED_PREQUAL_RULES:
        if r["match"] in upper:
            return r
    return None


def search_pdf(text):
    """Return (verdict, evidence) where verdict ∈ {'yes','no','unknown'}."""
    if not text:
        return "unknown", ""
    flat = re.sub(r"\s+", " ", text).strip()
    flat_lc = flat.lower()

    # Strong negatives win over weak positives — a spec that says "prequal
    # not required" should NOT show as required just because the word is mentioned.
    for pat in NEGATIVE_PHRASES:
        m = re.search(pat, flat_lc, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 80)
            end = min(len(flat), m.end() + 80)
            return "no", flat[start:end]

    for pat in POSITIVE_PHRASES:
        m = re.search(pat, flat_lc, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 80)
            end = min(len(flat), m.end() + 80)
            return "yes", flat[start:end]

    # Weak signal — only bumps to "yes" if we're confident this IS a Div-00
    # / instructions-to-bidders document. Heuristic: page mentions "bidder"
    # AND "prequalif" near each other.
    for pat in WEAK_POSITIVE:
        m = re.search(pat, flat_lc)
        if m and "bidder" in flat_lc:
            start = max(0, m.start() - 80)
            end = min(len(flat), m.end() + 80)
            return "yes", flat[start:end]

    return "unknown", ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Path to 02- ESTIMATING folder")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on bids scanned (0 = all)")
    parser.add_argument(
        "--bids-json",
        default=str(Path(__file__).resolve().parent.parent / "src" / "assets" / "bids-data.json"),
        help="Path to bids-data.json — used to filter to currently active bids only.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan EVERY EST# folder, not just active bids. Off by default.",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        raise SystemExit(f"Root not found: {root}")

    active = None
    if not args.all:
        active = load_active_est_numbers(Path(args.bids_json))
        if active is None:
            print(f"[warn] no active-bid filter applied (couldn't read {args.bids_json}); scanning everything.")
        else:
            print(f"Filtering to {len(active)} currently-active bids (Bidding + Bid Or Bail) from bids-data.json.")

    bids = find_bid_folders(root, active_only_set=active)
    if args.limit:
        bids = bids[: args.limit]

    print(f"Scanning {len(bids)} bid folders for prequal language...")
    out = {}
    started = dt.datetime.utcnow().isoformat() + "Z"
    total_fetched = 0
    for i, (est, folder) in enumerate(bids, 1):
        # Hard-coded rule wins over PDF scanning. If the project owner has
        # a known prequal posture (e.g. SHC = no), we don't waste cycles
        # scanning specs and we don't risk a false positive.
        rule = hardcoded_prequal_for(folder.name)
        if rule:
            out[est] = {
                "prequal_required": rule["verdict"],
                "evidence": rule["evidence"],
                "source_file": "",
                "scanned_pdfs": 0,
                "fetched_from_cloud": 0,
                "scanned_at": started,
                "rule_applied": rule["match"],
            }
            print(f"  [{i:>2}/{len(bids)}] {est:>8}  {rule['verdict']:>7}  (rule: {rule['match']})")
            continue
        candidates = candidate_specs(folder)
        scanned = 0
        fetched = 0
        verdict = "unknown"
        evidence = ""
        source_file = ""
        for pdf, allow_fetch in candidates:
            cloud = is_cloud_only(pdf)
            if cloud and not allow_fetch:
                continue
            if cloud:
                fetched += 1
                total_fetched += 1
            text, err = extract_text(pdf)
            scanned += 1
            if err or not text:
                continue
            v, e = search_pdf(text)
            if v == "yes":
                verdict = "yes"
                evidence = e.strip()
                source_file = str(pdf.relative_to(folder))
                break
            if v == "no" and verdict != "yes":
                verdict = "no"
                evidence = e.strip()
                source_file = str(pdf.relative_to(folder))
        out[est] = {
            "prequal_required": verdict,
            "evidence": evidence[:280],
            "source_file": source_file,
            "scanned_pdfs": scanned,
            "fetched_from_cloud": fetched,
            "scanned_at": started,
        }
        fetch_note = f" +{fetched} fetched" if fetched else ""
        print(f"  [{i:>2}/{len(bids)}] {est:>8}  {verdict:>7}  ({scanned} pdfs scanned{fetch_note})")
    print(f"\nTotal cloud fetches across this run: {total_fetched}")

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "bids": out,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote prequal map to {out_path}")

    # Also emit a .js sibling that the portal frontend can <script>-tag
    # without needing a fetch. Convert-Prequal.ps1 mirrors this into the
    # BAY Bid List deploy folder.
    js_path = out_path.with_suffix(".js")
    js_lines = [
        "// AUTO-GENERATED — regenerate with fusion-portal/scripts/scan-prequal.py",
        "window.__STATIC_PREQUAL__ = " + json.dumps(payload, separators=(",", ":")) + ";",
        "window.getStaticPrequal = function(estNumber){",
        "  var p = window.__STATIC_PREQUAL__;",
        "  if (!p || !p.bids || !estNumber) return null;",
        "  return p.bids[String(estNumber).toUpperCase()] || null;",
        "};",
        "window.getStaticPrequalMeta = function(){",
        "  var p = window.__STATIC_PREQUAL__;",
        "  return p ? { generated_at: p.generated_at, count: Object.keys(p.bids || {}).length } : null;",
        "};",
    ]
    js_path.write_text("\n".join(js_lines) + "\n", encoding="utf-8")
    print(f"Wrote prequal .js to  {js_path}")

    yes = sum(1 for v in out.values() if v["prequal_required"] == "yes")
    no = sum(1 for v in out.values() if v["prequal_required"] == "no")
    unk = sum(1 for v in out.values() if v["prequal_required"] == "unknown")
    print(f"Summary: {yes} yes / {no} no / {unk} unknown")


if __name__ == "__main__":
    main()
