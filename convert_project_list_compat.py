"""Import shim for convert-project-list.py.

Same pattern as the bid + budget shims: importlib loads the hyphenated
module so cloud-sync-project-list.py can call its parse_workbook()
directly. Any future change to the local parser flows through with no
edits here.
"""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "convert-project-list.py"

if not _TARGET.is_file():
    raise ImportError(f"convert-project-list.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("convert_project_list", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

parse_workbook = _module.parse_workbook
