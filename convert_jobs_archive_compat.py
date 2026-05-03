"""Importlib shim for convert-jobs-archive.py."""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "convert-jobs-archive.py"

if not _TARGET.is_file():
    raise ImportError(f"convert-jobs-archive.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("convert_jobs_archive", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

parse_workbook = _module.parse_workbook
