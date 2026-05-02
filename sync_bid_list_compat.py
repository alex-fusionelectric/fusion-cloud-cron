"""Import shim for sync-bid-list.py.

Python won't `import sync-bid-list` because hyphens aren't valid in
module names, but renaming the file would break every existing caller
(AutoUpdate-Task.ps1, Sync-PortalData.ps1, etc.). So we load the
hyphenated module via importlib and re-export the helpers cloud-side
parsers need.

Adding a new function to sync-bid-list.py -- you may need to re-export
it here. Changing an existing function's signature -- it'll flow through
automatically with no edits, that's the whole point.
"""

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "sync-bid-list.py"

if not _TARGET.is_file():
    raise ImportError(f"sync-bid-list.py not found next to compat shim at {_TARGET}")

_spec = importlib.util.spec_from_file_location("sync_bid_list", _TARGET)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

# Re-exports used by cloud-sync-bid-list.py. Keep this list narrow; the
# wider it is, the more places we have to touch when the parser evolves.
extract_estimators            = _module.extract_estimators
build_pe_map                  = _module.build_pe_map
build_estimator_division_map  = _module.build_estimator_division_map
division_from_estimator       = _module.division_from_estimator
parse_bids                    = _module.parse_bids
parse_follow                  = _module.parse_follow
parse_archive                 = _module.parse_archive
