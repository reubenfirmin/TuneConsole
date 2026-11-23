"""Move paid-description access state to the existing settings owner."""
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'fold_description_access_settings'
TABLE = 'album_description_access'
PREFIX = 'album-description:access:'


def install(conn, _context):
    conn.execute(f'''
        INSERT INTO settings(key,value)
        SELECT ? || scope || ':' || credential_key,
               json_object('status',status,'retry_at',retry_at,'updated_at',updated_at,
                           'evidence_filename',evidence_filename,'evidence_digest',evidence_digest)
        FROM {TABLE}
    ''', (PREFIX,))
    conn.execute(f'DROP TABLE {TABLE}')


def validate(conn):
    if TABLE in objects(conn, (TABLE,)):
        raise MigrationError('Retired paid description access table is present')
