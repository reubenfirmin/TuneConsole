"""Retain policy history as one small setting per immutable policy version."""
import json

from .. import policy_history_read as history, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _restore_foreign_keys


NAME = 'store_archived_policy_references_in_settings'
RETIRED = 'recommendation_policy_history'
IMPRESSIONS = 'rec_served_impressions'
CAPTURE = 'recommendation_impression_policy_capture'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('''SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL
        AND (name IN (?,?) OR tbl_name=?) ORDER BY type,name''', (IMPRESSIONS, CAPTURE, RETIRED))]


def install(conn, _context):
    columns = ','.join(history.FIELDS)
    for version, *entry in conn.execute(f'SELECT version,{columns} FROM {RETIRED}'):
        key = history.PREFIX + version
        if worker_settings.read(conn, key) is not None:
            raise MigrationError(f'Archived policy setting already exists for {version!r}')
        worker_settings.save(conn, key, json.dumps(entry, separators=(',', ':'), allow_nan=False))
    serial = conn.execute(f'''SELECT max(COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0),
        COALESCE(max(id),0),COALESCE(max(archive_file_id),0)) FROM {RETIRED}''', (RETIRED,)).fetchone()[0]
    if serial:
        worker_settings.save(conn, history.NEXT_ARCHIVE_FILE, str(serial))
    conn.execute(history.view())
    old = f'SELECT version,{columns} FROM {RETIRED}'
    new = f'SELECT version,{columns} FROM {history.VIEW}'
    if conn.execute(f'{old} EXCEPT {new}').fetchone() or conn.execute(f'{new} EXCEPT {old}').fetchone():
        raise MigrationError('Archived policy inventory changed during migration')

    saved = definitions(conn, (IMPRESSIONS, CAPTURE))
    reference = f' REFERENCES {RETIRED}(version)'
    if saved[IMPRESSIONS].count(reference) != 2:
        raise MigrationError('Unexpected historical policy foreign keys on served impressions')
    impressions = saved[IMPRESSIONS].replace(reference, '')
    capture = saved[CAPTURE].replace('p.version,h.version,',
                                    'p.version,CASE WHEN h.key IS NOT NULL THEN a.version END,')
    capture = capture.replace('h.version IS NULL', 'h.key IS NULL')
    capture = capture.replace(f'LEFT JOIN {RETIRED} h', 'LEFT JOIN settings h')
    capture = capture.replace('h.version=a.version', f"h.key='{history.PREFIX}'||a.version")
    conn.execute(f'DROP TRIGGER {CAPTURE}')
    _restore_foreign_keys(conn, ((impressions, IMPRESSIONS),))
    conn.execute(capture)
    conn.execute(f'DROP TABLE {RETIRED}')

    prefix, pattern = history.PREFIX, history.PATTERN
    conn.execute(f'''CREATE UNIQUE INDEX ix_archived_policy_file ON settings(value->>'$[3]')
        WHERE key GLOB '{pattern}' AND value->>'$[3]' IS NOT NULL''')
    conn.execute(f'''CREATE INDEX ix_archived_policy_expiry ON settings(value->>'$[1]',key)
        WHERE key GLOB '{pattern}' AND value->>'$[2]'='archived' ''')
    for role in ('primary', 'pool'):
        column = f'{role}_historical_version'
        conn.execute(f'''CREATE TRIGGER {column}_requires_archive BEFORE UPDATE OF {column} ON {IMPRESSIONS}
            WHEN NEW.{column} IS NOT NULL AND NOT EXISTS(SELECT 1 FROM settings WHERE key='{prefix}'||NEW.{column})
            BEGIN SELECT RAISE(ABORT,'Historical policy reference requires an archive entry'); END''')
    version = f'substr(OLD.key,{len(prefix) + 1})'
    conn.execute(f'''CREATE TRIGGER retained_policy_archive_delete BEFORE DELETE ON settings
        WHEN OLD.key GLOB '{pattern}' AND (EXISTS(SELECT 1 FROM {IMPRESSIONS} WHERE primary_historical_version={version})
              OR EXISTS(SELECT 1 FROM {IMPRESSIONS} WHERE pool_historical_version={version}))
        BEGIN SELECT RAISE(ABORT,'Historical policy attribution retains this archive entry'); END''')


def validate(conn):
    if set(definitions(conn, (RETIRED, history.VIEW))) != {history.VIEW}:
        raise MigrationError('Archived policy references were not moved to settings')
