"""Read-only installed-format admission shared by copying and recovery."""
from ..migrations import regular
from ..migrations.registry import MIGRATIONS
from ..migrations.runner import history, validate_version


def validate(conn):
    version = history(conn, MIGRATIONS)
    if version < 79:
        raise ValueError('Retained-copy compaction requires a supported installed format (79 or later)')
    validate_version(conn, version, foreign_keys=False, data=False)
    regular.validate(conn)
    return version
