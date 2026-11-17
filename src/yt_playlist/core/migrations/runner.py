"""Current-release bootstrap and ordinary, bounded schema changes."""
import json
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from yt_playlist.core.schema_contract import MigrationError

# v0.1.19, published 2026-11-12: earlier installations reinitialize locally.
RELEASE_VERSION = 107
RELEASE_CHANGES = 37
NOTICE = 'early_adopter_notice'


@dataclass(frozen=True)
class Context:
    now: float


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable | None = None
    rebuild_tables: bool = False


def history(conn, registry):
    table = conn.execute("SELECT type FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    if table is None:
        return 0
    columns = conn.execute('PRAGMA table_info(schema_migrations)').fetchall()
    if (table[0] != 'table' or [(r[1], r[2].upper(), r[3], r[5]) for r in columns] !=
            [('version', 'INTEGER', 0, 1), ('name', 'TEXT', 1, 0), ('applied_at', 'REAL', 1, 0)]):
        raise MigrationError('Incompatible schema_migrations layout')
    rows = conn.execute('SELECT version,name,applied_at FROM schema_migrations ORDER BY version').fetchall()
    if len(rows) > len(registry):
        raise MigrationError('Database schema is newer than this application')
    for row, migration in zip(rows, registry):
        if row[0] != migration.version or row[1] != migration.name or not isinstance(row[2], (int, float)):
            raise MigrationError('Migration history is not the supported contiguous prefix')
    return len(rows)


def validate_version(conn, version, *, foreign_keys=True, data=True):
    from . import current
    if version != RELEASE_VERSION:
        raise MigrationError('This installed layout requires reinitialization')
    current.validate(conn, foreign_keys=foreign_keys, data=data)


def _release_changes(conn):
    """Read the ledger before choosing reset; unknown/newer histories fail closed."""
    from . import regular
    from ..schema_contract import tokens
    row = conn.execute("SELECT type,sql FROM sqlite_schema WHERE name='schema_changes'").fetchone()
    if row is None:
        return 0
    if row[0] != 'table' or tokens(row[1]) != tokens(regular.HISTORY_SQL):
        raise MigrationError('Incompatible schema_changes layout')
    return history_table(conn, 'schema_changes', regular.CHANGES)


def history_table(conn, table, registry):
    rows = conn.execute('SELECT version,name,applied_at FROM '+table+' ORDER BY version').fetchall()
    if len(rows) > len(registry):
        raise MigrationError('Database schema is newer than this application')
    if any(row[0] != item.version or row[1] != item.name or not isinstance(row[2], (int, float))
           for row, item in zip(rows, registry)):
        raise MigrationError('Migration history is not the supported contiguous prefix')
    return len(rows)


def _account_bindings(conn):
    """Keep the IDs referenced by config files and durable playback observations."""
    required = ('id', 'label', 'credential_ref', 'brand_account_id', 'is_master')
    for table in ('accounts', 'identities'):
        columns = {row[1] for row in conn.execute('PRAGMA table_info('+table+')')}
        if set(required) <= columns:
            fields = required + tuple(name if name in columns else default for name, default in
                                      (('last_auth_ok', 'NULL'), ('active', '1')))
            return conn.execute('SELECT '+','.join(fields)+' FROM '+table+' ORDER BY id').fetchall()
    return []


def _initialize(conn, clock, *, reset=False):
    from . import current, regular
    from .registry import MIGRATIONS
    from .. import schema_bootstrap
    provenance = {}
    highwaters = []
    accounts, pairing = [], []
    if reset:
        accounts = _account_bindings(conn)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(settings)')}
        if {'key', 'value'} <= columns:
            pairing = conn.execute("SELECT key,value FROM settings WHERE key='bridge_paired'").fetchall()
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            highwaters = conn.execute('SELECT name,seq FROM sqlite_sequence').fetchall()
        for table in ('schema_migrations', 'schema_changes'):
            if conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone():
                provenance[table] = [tuple(row) for row in conn.execute('SELECT * FROM '+table)]
        # One SQLite transaction owns removal, bootstrap and the notice. A failed
        # bootstrap or interrupted process leaves the complete old database.
        objects = conn.execute("SELECT type,name FROM sqlite_schema WHERE sql IS NOT NULL "
            "AND type IN ('trigger','view','table') AND name NOT GLOB 'sqlite_*' "
            "ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'view' THEN 1 ELSE 2 END").fetchall()
        for kind, name in objects:
            conn.execute('DROP '+kind+' IF EXISTS "'+name.replace('"', '""')+'"')
    schema_bootstrap.bootstrap(conn, MIGRATIONS, regular.CHANGES, clock(), validation=current.validate)
    regular.validate(conn)
    if reset:
        conn.executemany('INSERT INTO accounts(id,label,credential_ref,brand_account_id,is_master,last_auth_ok,active) '
                         'VALUES (?,?,?,?,?,?,?)', accounts)
        conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', pairing)
        # File receipts use monotonically allocated SQL IDs. Orphaned files
        # from the discarded database must never acquire a new owner after reset.
        for table, value in highwaters:
            sql = conn.execute('SELECT sql FROM sqlite_schema WHERE type=\'table\' AND name=?', (table,)).fetchone()
            if sql and 'AUTOINCREMENT' in sql[0].upper():
                conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (table,))
                conn.execute('INSERT INTO sqlite_sequence VALUES (?,?)', (table, value))
        conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', (
            (NOTICE, uuid4().hex), ('early_adopter_previous_history', json.dumps(provenance))))


def migrate(conn, *, registry=None, clock=time.time, defer_validation=False):
    from . import current, regular
    from .registry import MIGRATIONS
    from ..schema_inspection import reuse_objects, reuse_tables
    if registry is not None and tuple(registry) != MIGRATIONS:
        raise MigrationError('Historical migration replay has been retired')
    if conn.in_transaction:
        raise MigrationError('Cannot migrate with an outstanding caller transaction')
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    try:
        with reuse_tables(conn) as inspections:
            conn.execute('BEGIN')
            version = history(conn, MIGRATIONS)
            changes = _release_changes(conn)
            fresh = not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                                     "AND name NOT GLOB 'sqlite_*' LIMIT 1").fetchone()
            if not fresh and version == RELEASE_VERSION and changes >= RELEASE_CHANGES:
                with reuse_objects(conn):
                    validate_version(conn, version, foreign_keys=False, data=False)
                    regular.validate(conn)
                conn.rollback()
                if not defer_validation and changes < len(regular.CHANGES):
                    regular.migrate(conn, clock=clock)
                return False
            conn.rollback()
            # Disable FK actions before the reset transaction; DROP must never
            # cascade into another owner while replacing the old layout.
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('BEGIN IMMEDIATE')
            inspections.refresh()
            fresh = not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                                     "AND name NOT GLOB 'sqlite_*' LIMIT 1").fetchone()
            # Recheck under the write reservation in case another startup won.
            if history(conn, MIGRATIONS) == RELEASE_VERSION and _release_changes(conn) >= RELEASE_CHANGES:
                conn.rollback()
                return migrate(conn, clock=clock, defer_validation=defer_validation)
            _initialize(conn, clock, reset=not fresh)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise MigrationError('Invalid initialized foreign-key reference')
            conn.commit()
            return False
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys='+str(foreign_keys))
