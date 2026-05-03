"""Scan a project's spec PDFs for CSI divisions Fusion bids on, then map
them to recommended Fusion scopes (FA, LV, LTNG, DIST, AV, SEC, NC, AIC,
TRNCH).

Background:
- CSI MasterFormat 2020 puts all our scopes in 3 divisions:
  - Division 26 — Electrical (DIST, LTNG, generators, switchgear, etc.)
  - Division 27 — Communications (LV/data/voice, AV in some specs)
  - Division 28 — Electronic Safety & Security (FA, SEC, intrusion, NC)
- Spec PDFs are usually a Table of Contents document and individual section
  files like "26 05 00 - Common Work Results for Electrical.pdf". The
  scanner looks for either format.

Output: state/divisions-data.json keyed by est_number with structure:
  {
    "divisions": ["26", "27"],
    "sections": ["260010", "260513", "270500", ...],
    "recommended_scopes": ["distribution", "lighting", "low voltage"],
    "evidence": "Found Section 26 05 13 - Medium-Voltage Cables in 26 0000 TOC.pdf",
    "scanned_pdfs": 3,
    "scanned_at": "..."
  }

The Bay Bid List page reads this file to surface a "Recommended scopes"
hint per project so estimators can verify they've covered every division
that actually appears in the specs.
"""

import argparse
import datetime as dt
import json
import re
import stat
import sys
from pathlib import Path

# Pure-Python PDF text extraction.
try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    print("[error] pypdf not installed — pip install pypdf", file=sys.stderr)
    sys.exit(2)

# Section-number regex covering the most common encodings:
#   "26 05 00", "26 0500", "260500", "26-05-00"
SECTION_RE = re.compile(r"\b(2[678])[\s\-]*(\d{2})[\s\-]*(\d{2})\b")

# Map of (CSI-section-prefix → recommended Fusion scope). Prefix can be
# 4-digit (26 05) or 6-digit (26 05 13). Most specific match wins.
# Sources: CSI MasterFormat 2020 + Fusion's typical scope assignments.
SECTION_TO_SCOPE = {
    # Division 26 — Electrical
    "26": "distribution",            # default for any 26 section
    "2605": "distribution",          # Common Work Results
    "260513": "distribution",        # Medium-Voltage Cables
    "260519": "distribution",        # Low-Voltage Conductors
    "260526": "distribution",        # Grounding & Bonding
    "260533": "distribution",        # Raceways & Boxes
    "260543": "trenching",           # Underground Ductbanks
    "260573": "distribution",        # Coordination Studies
    "262200": "distribution",        # Low-Voltage Transformers
    "262300": "distribution",        # Low-Voltage Switchgear
    "262400": "distribution",        # Switchboards & Panelboards
    "262500": "distribution",        # Enclosed Bus Assemblies
    "262600": "distribution",        # Power Distribution Units
    "262700": "distribution",        # Low-Voltage Distribution Equipment
    "262813": "distribution",        # Fuses
    "262816": "distribution",        # Enclosed Switches & Circuit Breakers
    "262900": "distribution",        # Low-Voltage Controllers
    "263213": "distribution",        # Engine Generators
    "263500": "distribution",        # Power Filtering & Conditioning
    "263600": "distribution",        # Transfer Switches
    "264313": "distribution",        # Surge Protective Devices
    "264500": "distribution",        # Cathodic Protection
    "265000": "lighting",            # Lighting (catch-all for 26 5x)
    "265100": "lighting",            # Interior Lighting
    "265600": "lighting",            # Exterior Lighting
    "266000": "lighting",            # Lighting controls (sometimes)

    # Division 27 — Communications
    "27": "low voltage",             # default for any 27 section
    "2705": "low voltage",           # Common Work Results
    "270500": "low voltage",         # Common Work Results
    "270526": "low voltage",         # Grounding & Bonding (LV)
    "270528": "low voltage",         # Pathways
    "271000": "low voltage",         # Structured Cabling
    "271100": "low voltage",         # Communications Equipment Room
    "271300": "low voltage",         # Communications Backbone Cabling
    "271500": "low voltage",         # Communications Horizontal Cabling
    "272100": "low voltage",         # Data Communications Network Equipment
    "274100": "audio visual",        # Audio Video Systems
    "274200": "audio visual",        # Electronic Digital Signage
    "275116": "nurse call",          # Public Address & Mass Notification (overlap; classify as NC if hospital context)
    "275123": "nurse call",          # Hospital Communications (Nurse Call)
    "275200": "nurse call",          # Healthcare Communications

    # Division 28 — Electronic Safety & Security
    "28": "security",                # default for any 28 section (SEC most common)
    "2805": "security",              # Common Work Results
    "281000": "security",            # Access Control
    "281300": "security",            # Access Control Hardware
    "282000": "security",            # Video Surveillance
    "282300": "security",            # Video Management Systems
    "283100": "fire alarm",          # Fire Detection & Alarm
    "283111": "fire alarm",          # Fire Alarm
    "283113": "fire alarm",          # Fire Alarm
    "283139": "fire alarm",          # Mass Notification
    "284600": "security",            # Electronic Personal Safety
}


def extract_text(pdf_path):
    """Return up to ~80k chars of text from the PDF, or empty string on
    failure. We don't need full text — section numbers usually appear on
    the cover / TOC of the spec set."""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:  # noqa: BLE001
        return ""
    return _read_reader(reader)


def extract_text_from_bytes(pdf_bytes):
    """Cloud variant: take raw PDF bytes (Dropbox download) and return
    up-to-50-pages of extracted text. Same return contract as extract_text():
    text or empty string. Used by cloud-sync-divisions.py."""
    import io
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:  # noqa: BLE001
        return ""
    return _read_reader(reader)


def _read_reader(reader):
    out = []
    for page in reader.pages[:50]:  # first 50 pages cover TOCs
        try:
            out.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
        if sum(len(s) for s in out) > 80_000:
            break
    return "\n".join(out)


def find_bid_folders(root, active_only=None):
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        m = re.match(r"^EST#\s*([0-9]{2}-[0-9]{3,4})\b", entry.name, re.IGNORECASE)
        if not m:
            continue
        est = m.group(1).upper()
        if active_only is not None and est not in active_only:
            continue
        out.append((est, entry))
    return out


def candidate_specs(folder):
    """Return spec-PDF candidates inside the project folder. Same heuristic
    as scan-prequal.py — any PDF whose name suggests it's a spec / TOC /
    project manual is fair game."""
    out = []
    if not folder.is_dir():
        return out
    for pdf in folder.rglob("*.pdf"):
        name = pdf.name.lower()
        if any(k in name for k in [
            "spec", "specs", "specifications", "manual",
            "table of contents", "toc",
            "26 05", "26 00", "27 00", "28 00",
            "div 26", "div 27", "div 28",
        ]):
            out.append(pdf)
    # Cap to first 6 to avoid scanning every drawing.
    return out[:6]


def is_cloud_only(path):
    """Skip Dropbox-cloud-only files so we don't pull every spec to disk."""
    try:
        attrs = path.stat().st_file_attributes
        FILE_ATTRIBUTE_OFFLINE = 0x00001000
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
        return bool(attrs & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))
    except Exception:  # noqa: BLE001
        return False


def scan_pdf_for_sections(text):
    """Parse a chunk of PDF text and return the set of canonical 6-digit
    section keys it mentions. '26 05 13' and '260513' both normalize to
    '260513'."""
    found = set()
    for m in SECTION_RE.finditer(text):
        key = "".join(m.groups())
        found.add(key)
    return found


def map_sections_to_scopes(sections):
    """Given a set of '260513'-style keys, return the unique Fusion scopes
    those sections imply. Matches by longest prefix first so 283100 (FA)
    wins over the generic '28' (SEC)."""
    out = []
    for sec in sections:
        match = None
        for n in (6, 4, 2):
            prefix = sec[:n]
            if prefix in SECTION_TO_SCOPE:
                match = SECTION_TO_SCOPE[prefix]
                break
        if match and match not in out:
            out.append(match)
    return out


def load_active_est_numbers(bids_json_path):
    if not bids_json_path or not bids_json_path.is_file():
        return None
    try:
        data = json.loads(bids_json_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    out = set()
    for b in (data.get("bids") or []):
        status = str(b.get("status") or "").strip().lower()
        if status not in {"bidding", "bid or bail"}:
            continue
        est = str(b.get("estNumber") or "").strip().upper()
        if est:
            out.add(est)
    return out or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Path to '02- ESTIMATING' Dropbox folder containing EST# subfolders.")
    parser.add_argument("--out", required=True, help="Output path for divisions-data.json.")
    parser.add_argument("--js-out", default="", help="Optional sibling .js wrapper output path.")
    parser.add_argument("--bids-json", default="", help="Path to bids-data.json — used to filter to currently-active bids only.")
    parser.add_argument("--limit", type=int, default=0, help="Cap to N bids for testing.")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"[error] root folder not found: {root}", file=sys.stderr)
        return 2

    active = None
    if args.bids_json:
        active = load_active_est_numbers(Path(args.bids_json))
        if active:
            print(f"Filtering to {len(active)} currently-active bids.")

    bids = find_bid_folders(root, active_only=active)
    if args.limit:
        bids = bids[: args.limit]

    print(f"Scanning {len(bids)} bid folders for CSI divisions...")
    out = {}
    started = dt.datetime.now().isoformat(timespec="seconds")
    for i, (est, folder) in enumerate(bids, 1):
        candidates = candidate_specs(folder)
        scanned = 0
        all_sections = set()
        first_evidence = ""
        for pdf in candidates:
            if is_cloud_only(pdf):
                continue
            text = extract_text(pdf)
            scanned += 1
            if not text:
                continue
            secs = scan_pdf_for_sections(text)
            if secs:
                if not first_evidence:
                    first_evidence = f"Found {len(secs)} CSI sections in {pdf.name}"
                all_sections.update(secs)
        scopes = map_sections_to_scopes(all_sections)
        divisions = sorted({s[:2] for s in all_sections if s[:2] in {"26", "27", "28"}})
        out[est] = {
            "divisions": divisions,
            "sections": sorted(all_sections),
            "recommended_scopes": scopes,
            "evidence": first_evidence,
            "scanned_pdfs": scanned,
            "scanned_at": started,
        }
        print(f"  [{i:>2}/{len(bids)}] {est:>8}  divs={','.join(divisions) or '—':<8} scopes={','.join(scopes) or '—'}")

    payload = {"generated_at": started, "bids": out}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote divisions map to {out_path}")

    if args.js_out:
        js_path = Path(args.js_out)
        js_path.parent.mkdir(parents=True, exist_ok=True)
        js_body = (
            "// AUTO-GENERATED by scan-divisions.py — do not edit by hand.\n"
            "window.__STATIC_DIVISIONS__ = " + json.dumps(payload) + ";\n"
            "window.getStaticDivisions = function(){\n"
            "  var p = window.__STATIC_DIVISIONS__;\n"
            "  return (p && p.bids) || {};\n"
            "};\n"
            "window.getRecommendedScopes = function(estNumber){\n"
            "  var key = String(estNumber || '').toUpperCase();\n"
            "  var bids = (window.__STATIC_DIVISIONS__ || {}).bids || {};\n"
            "  var entry = bids[key];\n"
            "  return entry ? (entry.recommended_scopes || []) : [];\n"
            "};\n"
        )
        js_path.write_text(js_body, encoding="utf-8")
        print(f"Wrote JS wrapper to {js_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
