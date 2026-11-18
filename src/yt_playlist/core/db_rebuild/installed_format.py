"""Read-only installed-format admission shared by copying and recovery."""
from ..migrations import regular
from ..migrations.registry import MIGRATIONS
from ..migrations.runner import history, validate_version


def validate(conn):
    version = history(conn, MIGRATIONS)
    if version != 107:
        raise ValueError('Retained-copy compaction requires the current release baseline')
    validate_version(conn, version, foreign_keys=False, data=False)
    regular.validate(conn)
    return version
