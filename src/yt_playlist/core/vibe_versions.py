"""Exact vibe version identities; normalization happens in an empty rebuild only."""
from functools import lru_cache
import re

COLUMNS = {'measurements': 'model_version', 'predictions': 'policy_version',
           'recording_vibes': 'inputs_version'}


def definitions():
    yield 'vibe_versions', 'CREATE TABLE vibe_versions (id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE)'
    yield 'vibe_versions_immutable', ("CREATE TRIGGER vibe_versions_immutable BEFORE UPDATE ON vibe_versions "
        "BEGIN SELECT RAISE(ABORT,'Vibe version identities are immutable'); END")


def install(conn, context=None):
    for _, sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .migrations.current import _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise ValueError('Incompatible vibe version owner: ' + name)


def normalized(conn):
    return any(row[1] == 'model_version_id' for row in conn.execute('PRAGMA main.table_info(measurements)'))


def column(conn, table):
    return COLUMNS[table] + ('_id' if normalized(conn) else '')


def intern(conn, version):
    if not isinstance(version, str):
        raise ValueError('Vibe version must retain its exact text identity')
    conn.execute('INSERT INTO vibe_versions(value) VALUES (?) ON CONFLICT(value) DO NOTHING', (version,))
    return conn.execute('SELECT id FROM vibe_versions WHERE value=?', (version,)).fetchone()[0]


def stored(conn, version):
    return intern(conn, version) if normalized(conn) else version


def lookup(conn, version):
    if not normalized(conn):
        return version
    row = conn.execute('SELECT id FROM vibe_versions WHERE value=?', (version,)).fetchone()
    return row[0] if row else None


def expression(conn, table, alias):
    name = COLUMNS[table]
    return (f'(SELECT value FROM vibe_versions WHERE id={alias}.{name}_id)'
            if normalized(conn) else f'{alias}.{name}')


def projection(conn, table, alias):
    name = COLUMNS[table]
    return ','.join(expression(conn, table, alias) + ' AS ' + name if row[1] == name+'_id'
                    else alias+'.'+row[1] for row in conn.execute('PRAGMA main.table_info('+table+')'))


def transform(sql, table):
    name = COLUMNS[table]
    sql = re.sub(r'\b'+name+r'\b', name+'_id', sql)
    return sql.replace(name+'_id TEXT NOT NULL', name+'_id INTEGER NOT NULL REFERENCES vibe_versions(id)')


@lru_cache(maxsize=1)
def contract():
    from .migrations import current
    statements = []
    for sql in current.statements():
        match = re.match(r'\s*CREATE TABLE (measurements|predictions|recording_vibes)\b', sql, re.I)
        if match:
            statements.append(transform(sql, match[1]))
    return current._schema_contract(statements)


def normalize_empty(conn):
    """The rebuild owns an empty schema with its triggers already suspended."""
    if normalized(conn):
        return
    for table in COLUMNS:
        if conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
            raise ValueError('Vibe version conversion requires an empty rebuild destination')
    definitions = {table: conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (table,)).fetchone()[0]
                   for table in COLUMNS}
    indexes = [row[0] for row in conn.execute("SELECT sql FROM sqlite_schema WHERE type='index' "
               "AND tbl_name IN ('measurements','predictions','recording_vibes') AND sql IS NOT NULL")]
    for table in reversed(COLUMNS):
        conn.execute('DROP TABLE '+table)
    for table, sql in definitions.items():
        conn.execute(transform(sql, table))
    for sql in indexes:
        conn.execute(sql)
