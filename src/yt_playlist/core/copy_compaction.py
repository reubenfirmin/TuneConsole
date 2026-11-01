"""Compatibility entry point for retained-copy maintenance.

The implementation lives in core.db_rebuild. Diagnostic imports remain
available until their existing callers move to that package.
"""
from .db_rebuild.job import (
    _files, _owned_files, main, run,
)
from .db_rebuild.install import digest as _digest, reopen as _reopen, save as _save, recover


if __name__ == '__main__':
    raise SystemExit(main())
