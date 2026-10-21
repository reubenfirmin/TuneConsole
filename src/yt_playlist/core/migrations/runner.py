import logging
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Context:
    now: float


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable
    replaces_data: bool = True


def history(conn, registry):
    table = conn.execute("SELECT type FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    if table is None:
        return 0
    columns = list(conn.execute("PRAGMA table_info(schema_migrations)"))
    if (table[0] != 'table' or [(r[1], r[2].upper(), r[3], r[5]) for r in columns] !=
            [('version', 'INTEGER', 0, 1), ('name', 'TEXT', 1, 0), ('applied_at', 'REAL', 1, 0)]):
        raise MigrationError("Incompatible schema_migrations layout")
    rows = list(conn.execute("SELECT version,name,applied_at FROM schema_migrations ORDER BY version"))
    if len(rows) > len(registry):
        raise MigrationError("Database schema is newer than this application")
    for row, migration in zip(rows, registry):
        if row[0] != migration.version or row[1] != migration.name or not isinstance(row[2], (int, float)):
            raise MigrationError("Migration history is not the supported contiguous prefix")
    return len(rows)


def verified_backup(conn, source_version, target_version, backup_dir=None):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:  # in-memory stores have no original file to protect
        return None
    source = Path(path).resolve()
    destination = Path(backup_dir) if backup_dir is not None else source.parent / 'backups'
    destination.mkdir(parents=True, exist_ok=True)
    name = f"state-{source_version or 'legacy'}-to-{target_version}-{time.time_ns()}-{uuid.uuid4().hex[:8]}.db"
    backup = destination / name
    # Read the committed snapshot on another connection while BEGIN IMMEDIATE reserves the writer.
    # Calling backup() on the writing connection itself can wait forever.
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
            with closing(sqlite3.connect(backup)) as target:
                reader.backup(target)
                result = target.execute('PRAGMA integrity_check').fetchall()
                if result != [('ok',)]:
                    raise MigrationError(f"Backup failed integrity check: {backup}")
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    log.info("Verified migration backup: %s", backup)
    return backup


def validate_catalog(conn, version):
    from . import v008_recordings, v010_play_event_subjects
    validator = v010_play_event_subjects if version >= 10 else v008_recordings
    validator.validate(conn)
    if version >= 11:
        from . import v011_takeout_coverage
        v011_takeout_coverage.validate(conn)
    if version >= 12:
        from . import v012_enrichment_retries
        v012_enrichment_retries.validate(conn)
    if version >= 13:
        from . import v013_provider_coverage
        v013_provider_coverage.validate(conn)
    if version >= 14:
        from . import v014_artwork_retries
        v014_artwork_retries.validate(conn)
    if version >= 15:
        from . import v015_album_shares
        v015_album_shares.validate(conn)


def migrate(conn, *, registry=None, backup_dir=None, snapshot=verified_backup, clock=time.time):
    """Apply one version per transaction; preserve earlier commits if a later version fails.

    Store serializes its connection; SQLite's write reservation serializes independent startups.
    Tests may inject a registry and snapshot policy, without touching application data paths.
    """
    production = registry is None
    if production:
        from .registry import MIGRATIONS
        registry = MIGRATIONS
    registry = tuple(registry)
    if [m.version for m in registry] != list(range(1, len(registry) + 1)) or len({m.name for m in registry}) != len(registry):
        raise MigrationError("Invalid migration registry")
    if conn.in_transaction:
        raise MigrationError("Cannot migrate with an outstanding caller transaction")
    backed_up = False
    fresh = None
    while True:
        try:
            conn.execute('BEGIN IMMEDIATE')
            current = history(conn, registry)
            if fresh is None:
                fresh = not bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone())
            if production:
                from .v001_baseline import validate
                if current >= 8:
                    from . import v008_recordings
                    validate(conn, legacy=False, replaced=v008_recordings.REPLACED)
                    validate_catalog(conn, current)
                else:
                    validate(conn, legacy=current == 0)
            if current == len(registry):
                conn.rollback()
                return
            migration = registry[current]
            nonempty = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone())
            if nonempty and not fresh and not backed_up and migration.replaces_data:
                snapshot(conn, current, registry[-1].version, backup_dir)
                backed_up = True
            if current == 0:
                conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)')
            migration.apply(conn, Context(clock()))
            if not conn.in_transaction:
                raise MigrationError('Migration committed outside the runner')
            if production:
                if migration.version >= 8:
                    from . import v008_recordings
                    validate(conn, legacy=False, replaced=v008_recordings.REPLACED)
                    validate_catalog(conn, migration.version)
                else:
                    validate(conn, legacy=False)
            conn.execute('INSERT INTO schema_migrations VALUES (?,?,?)', (migration.version, migration.name, clock()))
            conn.commit()
            log.info('Applied migration %s: %s', migration.version, migration.name)
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            version = locals().get('migration')
            label = f'{version.version} ({version.name})' if version else 'validation/locking'
            raise MigrationError(f'Database migration {label} failed: {exc}') from exc
