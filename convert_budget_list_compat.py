"""Import shim for convert-budget-list.py (hyphenated module name).

Same trick as sync_bid_list_compat.py: load the hyphenated parser via
importlib so cloud-sync-budget-list.py can call its parse_workbook()
directly. Any future change to the local parser flows through with no
edits here.
"""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "convert-budget-list.py"

if not _TARGET.is_file():
    raise ImportError(f"convert-budget-list.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("convert_budget_list", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

# Single re-export -- parse_workbook is the only thing the cloud script needs.
parse_workbook = _module.parse_workbook
