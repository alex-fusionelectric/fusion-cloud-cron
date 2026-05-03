"""Importlib shim for convert-employees.py."""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "convert-employees.py"

if not _TARGET.is_file():
    raise ImportError(f"convert-employees.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("convert_employees", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

parse_workbook = _module.parse_workbook
