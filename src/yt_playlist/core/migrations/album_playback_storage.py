"""Keep the one pending album playback on its album."""
from ..album_playback_context import COLUMNS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_pending_album_playback'
RETIRED = 'album_playback_intents'
MARKER = 'uq_pending_album_playback'
DECLARATIONS = (
    'INTEGER REFERENCES accounts(id)',
    'TEXT',
    'TEXT REFERENCES playlist_scopes(id)',
    'TEXT',
    'TEXT',
    "REAL CHECK(playback_started_at IS NULL OR (typeof(playback_started_at) IN ('integer','real') "
    'AND abs(playback_started_at)<=1.7976931348623157e308))',
    "TEXT CHECK(playback_video_ids IS NULL OR (json_valid(playback_video_ids) AND json_type(playback_video_ids)='array'))",
)
REVISION = (
    'INTEGER CHECK((playback_revision IS NULL AND '
    + ' AND '.join(f'{column} IS NULL' for column in COLUMNS[:-1])
    + ") OR (typeof(playback_revision)='integer' AND playback_revision>=1 "
    'AND playback_source_browse_id IS NOT NULL AND playback_playlist_scope_id IS NOT NULL '
    'AND playback_opportunity_id IS NOT NULL AND playback_started_at IS NOT NULL))'
)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM main.sqlite_schema WHERE name='albums' OR "
        "(tbl_name=? AND sql IS NOT NULL) ORDER BY type,name", (RETIRED,))]


def install(conn, _context):
    for column, declaration in zip(COLUMNS, (*DECLARATIONS, REVISION), strict=True):
        conn.execute(f'ALTER TABLE albums ADD COLUMN {column} {declaration}')
    conn.execute('UPDATE albums SET (' + ','.join(COLUMNS) + ')=('
                 'intent.account_id,intent.source_browse_id,intent.playlist_scope_id,intent.opportunity_id,'
                 'intent.action_id,intent.started_at,intent.video_ids,intent.revision) '
                 'FROM album_playback_intents intent WHERE albums.id=intent.album_id')
    conn.execute('DROP TABLE ' + RETIRED)
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON albums((1)) WHERE playback_revision IS NOT NULL')
    for action, event in (('insert', 'INSERT'), ('update', 'UPDATE OF ' + ','.join(COLUMNS))):
        conn.execute(f'''CREATE TRIGGER album_playback_scope_{action} BEFORE {event} ON albums
            WHEN NEW.playback_revision IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM playlist_scopes s WHERE s.id=NEW.playback_playlist_scope_id
                AND (s.kind='shared' OR (s.kind='account' AND s.account_id=NEW.playback_account_id)))
            BEGIN SELECT RAISE(ABORT,'Album playback playlist belongs to another account'); END''')
    conn.execute('''CREATE TRIGGER retained_album_playback BEFORE DELETE ON albums
        WHEN OLD.playback_revision IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'Album still owns pending playback'); END''')


def validate(conn):
    installed = definitions(conn, (MARKER, RETIRED))
    if set(installed) != {MARKER}:
        raise MigrationError('Incompatible pending album playback storage')
