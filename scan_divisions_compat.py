"""Importlib shim for scan-divisions.py — re-exports the cloud-friendly
helpers so cloud-sync-divisions.py can `import scan_divisions_compat`."""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "scan-divisions.py"

if not _TARGET.is_file():
    raise ImportError(f"scan-divisions.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("scan_divisions", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

scan_pdf_for_sections = _module.scan_pdf_for_sections
map_sections_to_scopes = _module.map_sections_to_scopes
extract_text_from_bytes = _module.extract_text_from_bytes
SECTION_TO_SCOPE = _module.SECTION_TO_SCOPE
SECTION_RE = _module.SECTION_RE
