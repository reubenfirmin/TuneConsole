"""Store dismissed playlist merge suggestions with other preferences."""
import json

from ..playlist_scope_state import MERGE_DISMISSALS
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'own_playlist_merge_dismissals'
RETIRED = 'ignored_merges'


def install(conn, _context):
    dismissals = {signature: {'members': json.loads(members), 'created_at': created_at}
                  for signature, members, created_at in conn.execute(
                      'SELECT signature,members,created_at FROM ignored_merges ORDER BY rowid')}
    if dismissals:
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (MERGE_DISMISSALS, json.dumps(dismissals)))
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    if objects(conn, (RETIRED,)):
        raise MigrationError('Retired playlist merge storage remains')
