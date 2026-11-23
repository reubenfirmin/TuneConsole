"""Copy listening history for the C96 startup upgrade."""
import re

from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .runner import PreparationChanged
from .track_source_cutover import _columns


PREFIX = '_listening_fold_'
SETTINGS = 'migration:96:'
PAGE_SIZE = 256
EVENTS = 'play_events'
OBSERVATIONS = 'listening_observations'
OWNERS = (EVENTS, OBSERVATIONS)


def _cursor(table):
    return SETTINGS + table


def _pending(table):
    return _cursor(table) + ':'


def _create(conn):
    prior = definitions(conn, OWNERS)
    for owner in OWNERS:
        sql = re.sub(r'^CREATE TABLE "?\w+"?', f'CREATE TABLE {PREFIX}{owner}', prior[owner], count=1)
        if owner == OBSERVATIONS:
            sql = sql.replace(' REFERENCES listening_capture_receipts(id)', '')
        conn.execute(sql)
        conn.execute('INSERT INTO settings(key,value) VALUES (?,NULL)', (_cursor(owner),))
    conn.execute(f"ALTER TABLE {PREFIX}{EVENTS} ADD COLUMN day TEXT CHECK(day IS NULL OR "
                 "(date(day) IS NOT NULL AND date(day)=day))")
    conn.execute(f"ALTER TABLE {PREFIX}{EVENTS} ADD COLUMN sources TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(sources)='array')")


def _copy_page(conn, owner, ids):
    columns = _columns(conn, owner)
    fields = ','.join(f'p."{column}"' for column in columns)
    matches = 'p.id IN (' + ','.join('?' for _ in ids) + ')'
    if owner == EVENTS:
        projection = f"""SELECT {fields},c.day,(SELECT json_group_array(source) FROM (
            SELECT source FROM play_event_sources WHERE event_id=p.id ORDER BY source
        )) AS sources,c.timezone IS a.listening_timezone AS same_timezone
        FROM play_events p LEFT JOIN play_event_calendar c ON c.event_id=p.id
        JOIN accounts a ON a.id=p.account_id WHERE {matches}"""
        if conn.execute(f"SELECT 1 FROM ({projection}) WHERE day IS NULL OR NOT same_timezone OR sources='[]' "
                        "LIMIT 1", ids).fetchone():
            raise MigrationError('Exact plays require their account calendar and at least one source')
        select = f'SELECT {",".join(columns)},day,sources FROM ({projection})'
    else:
        if conn.execute(f'''SELECT 1 FROM listening_observations p
            LEFT JOIN listening_capture_receipts c ON c.id=p.capture_id WHERE {matches}
              AND p.capture_id IS NOT NULL AND (c.id IS NULL OR p.account_id IS NOT c.account_id
                OR p.first_observed_at IS NOT c.captured_at OR p.last_observed_at IS NOT c.captured_at)
            LIMIT 1''', ids).fetchone():
            raise MigrationError('Capture timestamp differs from its archived observation')
        select = f'SELECT {fields} FROM listening_observations p WHERE {matches}'
    conn.execute(f'INSERT INTO {PREFIX}{owner} {select}', ids)


def _scan_page(conn, owner, after):
    where = 'WHERE id>?' if after is not None else ''
    ids = [row[0] for row in conn.execute(f'SELECT id FROM {owner} {where} ORDER BY id LIMIT {PAGE_SIZE}',
                                         () if after is None else (after,))]
    if ids:
        _copy_page(conn, owner, ids)
    conn.execute('UPDATE settings SET value=? WHERE key=?', (ids[-1] if ids else 'done', _cursor(owner)))


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    cursors = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?)',
                                tuple(_cursor(owner) for owner in OWNERS)))
    if not cursors:
        _create(conn)
        return True
    for owner in OWNERS:
        if conn.execute('DELETE FROM settings WHERE key GLOB ?', (_pending(owner) + '*',)).rowcount:
            conn.execute(f'DELETE FROM {PREFIX}{owner}')
            conn.execute('UPDATE settings SET value=NULL WHERE key=?', (_cursor(owner),))
            return True
        after = cursors[_cursor(owner)]
        if after != 'done':
            _scan_page(conn, owner, after)
            return True
    return False


def prepare(conn, version):
    more = True
    while more:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            more = _prepare_page(conn, version)


def swap(conn):
    cursors = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?)',
                                tuple(_cursor(owner) for owner in OWNERS)))
    if not cursors and not any(conn.execute(f'SELECT 1 FROM {owner} LIMIT 1').fetchone() for owner in OWNERS):
        _create(conn)
    elif len(cursors) != len(OWNERS) or any(cursor != 'done' for cursor in cursors.values()):
        raise PreparationChanged('Listening history preparation is incomplete')
    for owner in OWNERS:
        owned_sql = [sql for (sql,) in conn.execute(
            "SELECT sql FROM main.sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL "
            "AND type IN ('index','trigger') AND name NOT GLOB ? ORDER BY type,name",
            (owner, PREFIX + '*'))]
        highwater = _sequence(conn, owner) if owner == OBSERVATIONS else None
        conn.execute(f'DROP TABLE {owner}')
        conn.execute(f'ALTER TABLE {PREFIX}{owner} RENAME TO {owner}')
        if highwater is not None:
            _preserve_sequence(conn, owner, highwater)
        for sql in owned_sql:
            conn.execute(sql)
    conn.execute(f'DROP TRIGGER IF EXISTS {PREFIX}accounts_update')
    conn.execute('DELETE FROM settings WHERE key IN (?,?)', tuple(_cursor(owner) for owner in OWNERS))
