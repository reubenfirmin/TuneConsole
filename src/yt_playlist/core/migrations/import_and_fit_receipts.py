"""Keep import receipts and the last full fit version on their existing owners."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from ..sync_state import IMPORT_STAMPS


NAME = 'own_import_and_fit_receipts'
FIT = 'content_full_checkpoint'
SYNC = 'sync_attempts'


def predecessor_objects(conn):
    return list(definitions(conn, (FIT, SYNC)).values())


def install(conn, _context):
    conn.execute(f'ALTER TABLE {FIT} ADD COLUMN fit_version TEXT')
    conn.execute(f"UPDATE {FIT} SET fit_version=(SELECT value FROM settings WHERE key='rec_refresh_version') "
                 'WHERE singleton=1')
    for column in IMPORT_STAMPS:
        conn.execute(f'ALTER TABLE {SYNC} ADD COLUMN {column} TEXT')
    columns = ','.join(IMPORT_STAMPS)
    source = ','.join(f"max(value) FILTER (WHERE key='{column}')" for column in IMPORT_STAMPS)
    assignments = ','.join(f'{column}=excluded.{column}' for column in IMPORT_STAMPS)
    keys = tuple(IMPORT_STAMPS)
    marks = ','.join('?' for _ in keys)
    conn.execute(f"INSERT INTO {SYNC}(id,revision,phase,{columns}) SELECT 1,1,'running',{source} "
                 f'FROM settings WHERE key IN ({marks}) HAVING count(*)>0 '
                 'ON CONFLICT(id) DO UPDATE SET ' + assignments, keys)
    conn.execute(f'DELETE FROM settings WHERE key IN ({marks},?,?,?)',
                 (*keys, 'rec_refresh_version', 'trend_spotlight_review_month', 'enrich_attempts'))
    conn.execute("DELETE FROM settings WHERE key GLOB 'enrich_attempts:*'")


def validate(conn, *, sync_settings=False):
    schema = definitions(conn, (FIT, SYNC, 'sync_cursors', 'group_generation_worker_checkpoint'))
    sync = schema.get(SYNC, schema.get('sync_cursors'))
    fit = ('fit_version' in tokens(schema[FIT]) if FIT in schema else 'group_generation_worker_checkpoint' in schema)
    if (not fit
            or (not sync_settings and not set(IMPORT_STAMPS) <= set(tokens(sync)))):
        raise MigrationError('Incomplete import and fit receipts')
