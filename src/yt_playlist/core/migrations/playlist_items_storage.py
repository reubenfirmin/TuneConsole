"""Store each ordered playlist membership by track source."""
import json

from .. import group_preview_work, library_scope_changes, recording_library
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'unify_playlist_items'
TABLE = 'playlist_items'
RETIRED = 'unresolved_playlist_items'
MARKER = library_scope_changes.PLAYLIST_ITEMS_INDEX
_STAGE = '_playlist_items_preparation'
_CURSOR = 'migration:playlist-items:'
_PAGE_SIZE = 256


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "(tbl_name IN (?,?) OR name IN ('playlist_tracks','playlists_retire_empty')) ORDER BY type,name",
        (TABLE, RETIRED))]


def _source(table, alias):
    if table == TABLE:
        return f'(SELECT id FROM track_sources WHERE video_id={alias}.video_id)'
    return f'{alias}.entry_id'


def _create(conn):
    conn.execute(f'''CREATE TABLE {_STAGE} (
        playlist_id INTEGER NOT NULL REFERENCES playlists(id),
        track_source_id INTEGER NOT NULL REFERENCES track_sources(id),
        position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
        PRIMARY KEY(playlist_id,track_source_id),
        UNIQUE(playlist_id,position))''')
    for table in (TABLE, RETIRED):
        conn.execute('INSERT INTO settings(key,value) VALUES (?,NULL)', (_CURSOR + table,))


def _copy_page(conn, table, after):
    where = 'WHERE (p.playlist_id,p.position)>(?,?)' if after is not None else ''
    page = conn.execute(
        f'SELECT p.playlist_id,{_source(table, "p")},p.position FROM {table} p {where} '
        f'ORDER BY p.playlist_id,p.position LIMIT {_PAGE_SIZE}', json.loads(after) if after else ()).fetchall()
    # A checkpoint created by the old app can already contain later positions.
    conn.executemany(f'INSERT INTO {_STAGE} VALUES (?,?,?) ON CONFLICT(playlist_id,position) '
                     'DO UPDATE SET track_source_id=excluded.track_source_id', page)
    last = json.dumps([page[-1][0], page[-1][2]]) if page else 'done'
    conn.execute('UPDATE settings SET value=? WHERE key=?', (last, _CURSOR + table))


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    cursors = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?)',
                                (_CURSOR + TABLE, _CURSOR + RETIRED)))
    if not cursors:
        _create(conn)
        return True
    for table in (TABLE, RETIRED):
        after = cursors[_CURSOR + table]
        if after != 'done':
            _copy_page(conn, table, after)
            return True
    return False


def prepare(conn, version):
    more = True
    while more:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            more = _prepare_page(conn, version)


def install(conn, _context):
    cursors = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?)',
                                (_CURSOR + TABLE, _CURSOR + RETIRED)))
    if not cursors and not any(conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                               for table in (TABLE, RETIRED)):
        _create(conn)
    elif len(cursors) != 2 or any(cursor != 'done' for cursor in cursors.values()):
        raise PreparationChanged('Playlist item preparation is incomplete')
    triggers = conn.execute("SELECT name,sql FROM sqlite_schema WHERE tbl_name='playlist_items' "
                            "AND type='trigger' AND name NOT GLOB ? AND name NOT GLOB 'playlist_items_position_*' "
                            "AND name NOT GLOB 'group_preview_*' AND name NOT GLOB 'library_scope_*' "
                            "AND name NOT GLOB 'recording_library_*' ORDER BY name", (_STAGE + '*',)).fetchall()
    conn.execute('DROP VIEW playlist_tracks')
    conn.execute('DROP TRIGGER playlists_retire_empty')
    conn.execute('DROP TABLE unresolved_playlist_items')
    conn.execute('DROP TABLE playlist_items')
    conn.execute(f'ALTER TABLE {_STAGE} RENAME TO playlist_items')
    conn.execute(f'CREATE INDEX {MARKER} ON playlist_items(track_source_id,playlist_id)')
    conn.execute('CREATE VIEW playlist_tracks AS SELECT playlist_id,track_source_id AS track_id,position '
                 'FROM playlist_items')
    conn.execute('CREATE TRIGGER playlists_retire_empty BEFORE UPDATE OF is_current ON playlists '
                 'WHEN OLD.is_current=1 AND NEW.is_current=0 AND '
                 'EXISTS(SELECT 1 FROM playlist_items WHERE playlist_id=OLD.id) '
                 "BEGIN SELECT RAISE(ABORT,'Remove playlist tracks before retiring the membership'); END")
    for _, sql in triggers:
        conn.execute(sql.replace('OLD.video_id', 'OLD.track_source_id').replace('NEW.video_id', 'NEW.track_source_id'))
    for name, sql in library_scope_changes.triggers(
            folded=True, album_folded=True, listening_folded=True, playlist_items_folded=True):
        if name.startswith('library_scope_playlist_items_'):
            conn.execute(sql)
    for name, sql in recording_library.triggers(
            album_folded=True, listening_folded=True, playlist_items_folded=True):
        if name.startswith('recording_library_playlist_items_'):
            conn.execute(sql)
    group_preview_work.install_guards(conn, sources={TABLE})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')
    conn.execute('DELETE FROM settings WHERE key IN (?,?)', (_CURSOR + TABLE, _CURSOR + RETIRED))


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Incompatible playlist item storage')
