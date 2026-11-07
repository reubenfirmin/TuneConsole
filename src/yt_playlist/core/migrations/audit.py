"""Explicit, read-only integrity audit, independent of schema advancement."""
from dataclasses import dataclass
import time

from .runner import MigrationError, history, validate_version


@dataclass(frozen=True)
class AuditResult:
    version: int
    elapsed: float


def audit(conn, *, clock=time.monotonic):
    """Inspect one committed snapshot without reserving a writer or recording a migration.

    The caller owns the connection. In particular, do not borrow a serving
    connection: a full-library audit can be slow, and cancellation must only
    interrupt this reader. A failure leaves data and migration history intact.
    """
    from .registry import MIGRATIONS
    from . import regular

    if conn.in_transaction:
        raise MigrationError('Cannot audit with an outstanding caller transaction')
    started = clock()
    try:
        conn.execute('BEGIN')
        version = history(conn, MIGRATIONS)
        regular.validate(conn)
        validate_version(conn, version)
        return AuditResult(version, clock() - started)
    finally:
        conn.rollback()
