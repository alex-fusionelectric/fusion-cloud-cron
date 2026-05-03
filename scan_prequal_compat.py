"""Importlib shim for scan-prequal.py — re-exports the cloud-friendly
helpers (text classifier + hard-coded rule lookup + bytes->text extractor)
so cloud-sync-prequal.py can `import scan_prequal_compat`."""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "scan-prequal.py"

if not _TARGET.is_file():
    raise ImportError(f"scan-prequal.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("scan_prequal", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

search_pdf = _module.search_pdf
extract_text_from_bytes = _module.extract_text_from_bytes
hardcoded_prequal_for = _module.hardcoded_prequal_for
SPEC_FOLDER_HINTS = _module.SPEC_FOLDER_HINTS
SPEC_FILE_HINTS = _module.SPEC_FILE_HINTS
SKIP_FOLDER_HINTS = _module.SKIP_FOLDER_HINTS
HARDCODED_PREQUAL_RULES = _module.HARDCODED_PREQUAL_RULES
