"""Store saved-song sync progress with the other account cursors."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_saved_song_sync_cursor'
TABLE = 'sync_cursors'
RETIRED = 'song_library_sync'
SAVED_SONGS = "'saved_albums','saved_songs'"


def install(conn, _context):
    table = definitions(conn, (TABLE,))[TABLE]
    replacement = table.replace("'saved_albums'", SAVED_SONGS)
    if replacement == table or SAVED_SONGS in table:
        raise MigrationError('Unexpected sync cursor resource list')
    attached = [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') "
        "AND sql IS NOT NULL ORDER BY type,name", (TABLE,))]
    conn.execute('ALTER TABLE sync_cursors RENAME TO previous_sync_cursors')
    conn.execute(replacement)
    conn.execute('INSERT INTO sync_cursors SELECT * FROM previous_sync_cursors')
    conn.execute('''
        INSERT INTO sync_cursors(account_id,provider,collection,phase,revision,updated_at,last_success_at,failures,retry_at)
        SELECT account_id,'youtube','saved_songs',phase,1,updated_at,last_success_at,failures,retry_at
        FROM song_library_sync
    ''')
    conn.execute('DROP TABLE previous_sync_cursors')
    conn.execute('DROP TABLE song_library_sync')
    for sql in attached:
        conn.execute(sql)


def validate(conn):
    schema = definitions(conn, (TABLE, RETIRED))
    if RETIRED in schema or SAVED_SONGS not in schema.get(TABLE, ''):
        raise MigrationError('Saved-song sync progress was not consolidated')
