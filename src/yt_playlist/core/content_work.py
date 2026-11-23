"""Ordered content refreshes keyed by the captured recording identity."""
import re

from .db_revisions import setting_reads
from .worker_settings import increment_sql

PREFIX = 'content_pending:'
SEQUENCE = 'content_pending_serial'
INDEX = 'ix_content_pending_order'
RECORDING_INDEX = 'ix_recording_pending_content'
PENDING = f"key GLOB '{PREFIX}*'"
POSITION = 'value->>0'
REVISION = 'value->>1'
OFFSET = len(PREFIX) + 1
PAGE = 'SELECT identity_key,revision FROM content_refreshes ORDER BY position,identity_key LIMIT ?'
SERIAL = f"(SELECT CAST(value AS INTEGER) FROM settings WHERE key='{SEQUENCE}')"
_PREDECESSOR_ENQUEUE = re.compile(r'INSERT INTO rec_content_pending\(identity_key\)\s+(.*?)\s+'
                                  r'ON CONFLICT\(identity_key\) DO UPDATE SET revision=revision\+1;', re.DOTALL)


def enqueue_sql(selection, *, bump=True):
    conflict = ("DO UPDATE SET value=json_set(value,'$[1]',(value->>1)+1)"
                if bump else 'DO NOTHING')
    return (f"INSERT INTO settings(key,value) SELECT '{PREFIX}'||pending.subject,json_array(NULL,1) "
            f'FROM (WITH subjects(subject) AS ({selection}) SELECT subject FROM subjects) pending '
            f'WHERE pending.subject IS NOT NULL ON CONFLICT(key) {conflict}')


def upgrade_enqueue_sql(sql):
    return _PREDECESSOR_ENQUEUE.sub(lambda match: enqueue_sql(match[1]) + ';', sql)


_SETTINGS_ENQUEUE = re.compile(
    rf"INSERT INTO settings\(key,value\) SELECT '{PREFIX}'\|\|pending.subject,json_array\(NULL,1\) "
    r'FROM \(WITH subjects\(subject\) AS \((.*?)\) SELECT subject FROM subjects\) pending '
    r"WHERE pending.subject IS NOT NULL ON CONFLICT\(key\) DO UPDATE SET value=json_set\(value,'\$\[1\]',\(value->>1\)\+1\);",
    re.DOTALL,
)


def _queue(selection, bump):
    return (f'INSERT INTO content_refreshes(identity_key,bump) '
            f'SELECT subject,{int(bump)} FROM (WITH subjects(subject) AS ({selection}) SELECT subject FROM subjects) '
            'WHERE subject IS NOT NULL')


def upgrade_recording_enqueue_sql(sql):
    return _SETTINGS_ENQUEUE.sub(lambda match: _queue(match[1], True) + ';', sql)


def recording_queue_schema():
    return (
        f'CREATE INDEX {RECORDING_INDEX} ON recordings(content_pending_position,identity_key) '
        'WHERE content_pending_position IS NOT NULL',
        f'''CREATE VIEW content_refreshes(identity_key,position,revision,bump) AS
            SELECT identity_key,content_pending_position,content_pending_revision,NULL
            FROM recordings INDEXED BY {RECORDING_INDEX} WHERE content_pending_position IS NOT NULL
            UNION ALL
            SELECT substr(key,{OFFSET}),{POSITION},{REVISION},NULL FROM settings INDEXED BY {INDEX} WHERE {PENDING}''',
        f'''CREATE TRIGGER enqueue_content_refresh INSTEAD OF INSERT ON content_refreshes BEGIN
            {increment_sql(SEQUENCE)} AND (NEW.bump OR (
                NOT EXISTS(SELECT 1 FROM recordings WHERE identity_key=NEW.identity_key
                           AND content_pending_position IS NOT NULL)
                AND NOT EXISTS(SELECT 1 FROM settings WHERE key='{PREFIX}'||NEW.identity_key)));
            UPDATE recordings SET
                content_pending_position=COALESCE(content_pending_position,
                    (SELECT {POSITION} FROM settings WHERE key='{PREFIX}'||NEW.identity_key),{SERIAL}),
                content_pending_revision=CASE WHEN NEW.bump THEN {SERIAL} ELSE COALESCE(content_pending_revision,
                    (SELECT {REVISION} FROM settings WHERE key='{PREFIX}'||NEW.identity_key),{SERIAL}) END
            WHERE identity_key=NEW.identity_key AND (NEW.bump OR content_pending_position IS NULL);
            DELETE FROM settings WHERE key='{PREFIX}'||NEW.identity_key
                AND EXISTS(SELECT 1 FROM recordings WHERE identity_key=NEW.identity_key);
            INSERT INTO settings(key,value) SELECT '{PREFIX}'||NEW.identity_key,json_array({SERIAL},{SERIAL})
                WHERE NOT EXISTS(SELECT 1 FROM recordings WHERE identity_key=NEW.identity_key)
                ON CONFLICT(key) DO UPDATE SET value=json_set(value,'$[1]',{SERIAL}) WHERE NEW.bump;
        END''',
    )


def enqueue(conn, keys, *, bump=True):
    conn.executemany(_queue('SELECT ?', bump), ((key,) for key in keys))


def for_recording(conn, recording, *, bump=True):
    conn.execute(_queue('SELECT identity_key FROM recordings WHERE id=?', bump), (recording,))


def pending(conn, limit):
    with setting_reads(conn, (SEQUENCE,)):
        return list(map(tuple, conn.execute(PAGE, (limit,))))


def count(conn):
    return conn.execute('SELECT count(*) FROM content_refreshes').fetchone()[0]


def acknowledge(conn, captured):
    changed = 0
    for key, revision in captured:
        changed += conn.execute('UPDATE recordings SET content_pending_position=NULL,content_pending_revision=NULL '
                                'WHERE identity_key=? AND content_pending_revision=?', (key, revision)).rowcount
        changed += conn.execute(f'DELETE FROM settings WHERE key=? AND {REVISION}=?', (PREFIX + key, revision)).rowcount
    if changed:
        conn.execute(increment_sql(SEQUENCE))


def rotate(conn, captured):
    for key, _revision in captured:
        conn.execute(increment_sql(SEQUENCE))
        conn.execute(f'UPDATE recordings SET content_pending_position={SERIAL} '
                     'WHERE identity_key=? AND content_pending_position IS NOT NULL', (key,))
        conn.execute(f"UPDATE settings SET value=json_set(value,'$[0]',{SERIAL}) WHERE key=?", (PREFIX + key,))
