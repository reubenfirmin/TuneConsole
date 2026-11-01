"""The two explicitly supported rebuild inputs and their stable row cursors.

This is a read contract, not an upgrade runner. In particular, admission of the
released layout must never execute a populated historical migration.
"""
import hashlib
import json

from ..migrations import current, regular
from ..migrations.registry import MIGRATIONS
from ..migrations.runner import MigrationError, history, validate_version
from .build import quote


RELEASE_VERSION = 64
DEVELOPMENT_VERSION = 93
RELEASE_DDL = '95036d09b6a69976b6b6898bd94d1eef88b6b670b089198c9092491f00bfa8b7'


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                          "AND name NOT GLOB 'sqlite_*'")}


def admit(conn):
    """Check structure only, without a write reservation or library scan."""
    version = history(conn, MIGRATIONS)
    if version == RELEASE_VERSION:
        objects = [tuple(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]
        digest = hashlib.sha256(json.dumps(objects, separators=(',', ':')).encode()).hexdigest()
        if digest != RELEASE_DDL:
            raise MigrationError('Schema 64 does not match the supported v0.1.18 release')
    elif version == DEVELOPMENT_VERSION:
        validate_version(conn, version, foreign_keys=False, data=False)
        regular.validate(conn)
    else:
        raise MigrationError('Direct rebuild supports released schema 64 and the development schema 93')
    return version


def columns(conn, table):
    return [row[1] for row in conn.execute('PRAGMA main.table_xinfo('+quote(table)+')') if row[6] == 0]


def scan(conn, table, cursor, limit):
    """Read one primary-key batch, including tables with no declared row ID."""
    definition = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (table,)).fetchone()
    if definition is None:
        raise MigrationError('Missing rebuild input: '+table)
    if 'WITHOUT ROWID' in definition[0].upper():
        info = list(conn.execute('PRAGMA main.table_info('+quote(table)+')'))
        keys = [row[1] for row in sorted(info, key=lambda row:row[5]) if row[5]]
    else:
        names = {name.lower() for name in columns(conn, table)}
        keys = [next(key for key in ('rowid','_rowid_','oid') if key not in names)]
    expressions = ','.join(map(quote, keys))
    fields = columns(conn, table)
    query = 'SELECT '+expressions+','+','.join(map(quote, fields))+' FROM main.'+quote(table)
    parameters = []
    if cursor is not None:
        query += ' WHERE ('+expressions+')>('+','.join('?' for _ in keys)+')'
        parameters.extend(cursor)
    query += ' ORDER BY '+expressions+' LIMIT ?'
    rows = conn.execute(query, (*parameters, limit)).fetchall()
    values = [dict(zip(fields, tuple(row)[len(keys):])) for row in rows]
    bookmark = list(tuple(rows[-1])[:len(keys)]) if rows else cursor
    return values, bookmark, len(rows) < limit


def settings(conn, keys):
    return dict(conn.execute('SELECT key,value FROM main.settings WHERE key IN ('+
                            ','.join('?' for _ in keys)+')', tuple(keys))) if keys else {}
