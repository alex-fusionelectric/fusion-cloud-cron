"""Importlib shim for convert-tracking.py (hyphenated module name).

Same pattern as sync_bid_list_compat / convert_budget_list_compat:
loads the local script via importlib so cloud-sync-goals.py can call
parse_workbook directly without duplicating the parse logic.
"""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "convert-tracking.py"

if not _TARGET.is_file():
    raise ImportError(f"convert-tracking.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("convert_tracking", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

parse_workbook = _module.parse_workbook
