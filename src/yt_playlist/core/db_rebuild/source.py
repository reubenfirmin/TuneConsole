"""The two explicitly supported rebuild inputs and their stable row cursors.

This is a read contract, not an upgrade runner. In particular, admission of the
released layout must never execute a populated historical migration.
"""
from .. import released_schema
from ..migrations import regular
from ..migrations.registry import MIGRATIONS
from ..migrations.runner import MigrationError, history, validate_version
from .build import quote


RELEASE_VERSION = released_schema.VERSION
DEVELOPMENT_VERSION = 93
RELEASE_DDL = released_schema.DDL
MAX_BATCH_BYTES = 8 * 1024**2


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                          "AND name NOT GLOB 'sqlite_*'")}


def admit(conn):
    """Check structure only, without a write reservation or library scan."""
    version = history(conn, MIGRATIONS)
    if version == RELEASE_VERSION:
        released_schema.validate(conn)
    elif version == DEVELOPMENT_VERSION:
        validate_version(conn, version, foreign_keys=False, data=False)
        regular.validate(conn)
    else:
        raise MigrationError('Direct rebuild supports released schema 64 and the development schema 93')
    return version


def columns(conn, table):
    return [row[1] for row in conn.execute('PRAGMA main.table_xinfo('+quote(table)+')') if row[6] == 0]


def keys(conn, table):
    """Return the stable physical scan key for one explicitly admitted owner."""
    definition = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (table,)).fetchone()
    if definition is None:
        raise MigrationError('Missing rebuild input: '+table)
    if 'WITHOUT ROWID' in definition[0].upper():
        info = list(conn.execute('PRAGMA main.table_info('+quote(table)+')'))
        keys = [row[1] for row in sorted(info, key=lambda row:row[5]) if row[5]]
    else:
        names = {name.lower() for name in columns(conn, table)}
        keys = [next(key for key in ('rowid','_rowid_','oid') if key not in names)]
    return keys


def scan(conn, table, cursor, limit, *, legacy_id=False, predicate=None):
    """Read one key batch, bounded by both row count and captured byte size."""
    key_columns = keys(conn,table)
    expressions = ','.join(map(quote, key_columns))
    fields = columns(conn, table)
    query = 'SELECT '+expressions+','+','.join(map(quote, fields))+' FROM main.'+quote(table)+' s'
    parameters = []
    conditions = [predicate] if predicate else []
    if cursor is not None:
        conditions.append('('+expressions+')>('+','.join('?' for _ in key_columns)+')')
        parameters.extend(cursor)
    if conditions:
        query += ' WHERE '+' AND '.join('('+value+')' for value in conditions)
    query += ' ORDER BY '+expressions+' LIMIT ?'
    # Row counts alone do not bound captured JSON/vector batches. Keep one
    # original row intact, then stop at a small byte budget as well.
    rows, used = [], 0
    statement = conn.execute(query, (*parameters, limit))
    try:
        for row in statement:
            rows.append(row)
            used += sum(len(v.encode()) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 8 for v in row)
            if used >= 8*1024**2:
                break
    finally:
        statement.close()
    values = [dict(zip(fields, tuple(row)[len(key_columns):])) for row in rows]
    if legacy_id:
        if len(key_columns) != 1 or key_columns[0] in fields:
            raise MigrationError('Legacy capture requires a retained row ID: '+table)
        for value, row in zip(values, rows):
            value['_legacy_id'] = row[0]
    bookmark = list(tuple(rows[-1])[:len(key_columns)]) if rows else cursor
    return values, bookmark, len(rows) < limit and used < 8*1024**2


def window(conn, table, cursor, limit):
    """Find one byte-bounded SQL copy range without materializing its values."""
    key_columns = keys(conn, table)
    expressions = ','.join(map(quote, key_columns))
    sizes = ['CASE typeof('+quote(field)+") WHEN 'text' THEN length(CAST("+quote(field)
             +" AS BLOB)) WHEN 'blob' THEN length("+quote(field)+') ELSE 8 END'
             for field in columns(conn, table)]
    query = 'SELECT '+expressions+','+'+'.join(sizes)+' FROM main.'+quote(table)
    if cursor is not None:
        query += ' WHERE ('+expressions+')>('+','.join('?' for _ in key_columns)+')'
    query += ' ORDER BY '+expressions+' LIMIT ?'
    count, used, bookmark = 0, 0, cursor
    statement = conn.execute(query, (*(cursor or ()), limit))
    try:
        for row in statement:
            count += 1
            used += row[-1]
            bookmark = list(row[:-1])
            if used >= MAX_BATCH_BYTES:
                break
    finally:
        statement.close()
    return bookmark, count, count < limit and used < MAX_BATCH_BYTES


def settings(conn, keys):
    return dict(conn.execute('SELECT key,value FROM main.settings WHERE key IN ('+
                            ','.join('?' for _ in keys)+')', tuple(keys))) if keys else {}
