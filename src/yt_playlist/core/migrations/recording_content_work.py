"""Move pending content work from Settings to the recordings it describes."""
from .. import content_work
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'keep_pending_content_work_on_recordings'
RETIRED = ('content_pending_assign', 'content_pending_changed', 'content_pending_removed')


def _triggers(conn):
    return conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND instr(sql,?) ORDER BY name",
                        (f"'{content_work.PREFIX}'||pending.subject",)).fetchall()


def predecessor_objects(conn):
    return [*definitions(conn, ('recordings', *RETIRED)).values(), *(sql for _, sql in _triggers(conn))]


def _detach_pending(name, sql):
    # A removed identity retains its place as a retraction, even when the
    # same recording row now has a new identity and queues fresh work.
    detach = f'''INSERT INTO settings(key,value)
        SELECT '{content_work.PREFIX}'||OLD.identity_key,
               json_array(OLD.content_pending_position,OLD.content_pending_revision)
        WHERE OLD.content_pending_position IS NOT NULL'''
    if name == 'rec_content_recordings_update':
        detach += ''' AND OLD.identity_key IS NOT NEW.identity_key;
            UPDATE recordings SET content_pending_position=NULL,content_pending_revision=NULL
            WHERE id=NEW.id AND OLD.identity_key IS NOT NEW.identity_key'''
    return sql.replace('BEGIN', 'BEGIN ' + detach + ';', 1)


def install(conn, _context):
    from ..content_work import PENDING, POSITION, PREFIX, REVISION, SEQUENCE

    for name in RETIRED:
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute('ALTER TABLE recordings ADD COLUMN content_pending_position INTEGER')
    conn.execute('ALTER TABLE recordings ADD COLUMN content_pending_revision INTEGER')
    conn.execute(f'''UPDATE settings SET value=MAX(CAST(value AS INTEGER),COALESCE(
        (SELECT max(max({POSITION},{REVISION})) FROM settings WHERE {PENDING}),0)) WHERE key=?''', (SEQUENCE,))
    conn.execute(f'''UPDATE recordings AS r SET content_pending_position=p.position,content_pending_revision=p.revision
        FROM (SELECT key,{POSITION} AS position,{REVISION} AS revision FROM settings WHERE {PENDING}) p
        WHERE r.identity_key=substr(p.key,{content_work.OFFSET})''')
    conn.execute(f'''DELETE FROM settings WHERE {PENDING}
        AND EXISTS(SELECT 1 FROM recordings WHERE identity_key=substr(key,{content_work.OFFSET}))''')
    for sql in content_work.recording_queue_schema():
        conn.execute(sql)
    for name, sql in _triggers(conn):
        updated = content_work.upgrade_recording_enqueue_sql(sql)
        if sql == updated or 'pending.subject' in updated:
            raise MigrationError(f'Unknown content invalidation statement in {name}')
        if name in ('rec_content_recordings_update', 'rec_content_recordings_delete'):
            updated = _detach_pending(name, updated)
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(updated)


def validate(conn):
    names = (content_work.RECORDING_INDEX, 'content_refreshes', 'enqueue_content_refresh')
    if set(definitions(conn, (*names, *RETIRED))) != set(names):
        raise MigrationError('Incomplete recording content work cutover')
