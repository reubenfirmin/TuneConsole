"""Direct source imports use a fresh schema and preserve installed evidence."""
from contextlib import closing
import sqlite3

import pytest

from tests.db_rebuild_support import production64
from yt_playlist.core.db_rebuild import build, destination, source
from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import current, registry, runner
from yt_playlist.core.store import Store


@pytest.fixture
def development93(production64,tmp_path):
    # The numbered definition supplies this explicit second source. Replaying
    # every earlier bulk conversion would conceal a dependency on that chain.
    path = tmp_path/'development93.db'
    with closing(sqlite3.connect(path)) as conn, closing(sqlite3.connect(production64)) as released:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        for sql in current.statements(version=93):
            conn.execute(sql)
        genre_identity.seed_catalog(conn)
        conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
        conn.executemany('INSERT INTO schema_migrations VALUES (?,?,?)',
                         [(m.version,m.name,2000.) for m in registry.MIGRATIONS[:93]])
        conn.execute('INSERT INTO group_retention_state VALUES (1,0)')
        for table in ('identities','recordings','youtube_items','catalog_entries','library_memberships',
                      'playlist_scopes','playlists','playlist_items','item_ratings','play_events','play_event_sources',
                      'rec_change_queue','rec_preparation','rec_group_clock','rec_vibe_pending'):
            fields = source.columns(released,table)
            mapped = ['unresolved_genre' if field=='genre' else field for field in fields]
            conn.execute('DELETE FROM '+table)
            conn.executemany('INSERT INTO '+table+'('+','.join(mapped)+') VALUES ('+
                ','.join('?' for _ in fields)+')',released.execute('SELECT '+','.join(fields)+' FROM '+table))
        conn.commit()
        runner.validate_version(conn,93)
    return path


def test_release_admission_performs_no_writes(production64):
    with closing(sqlite3.connect(production64.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        assert source.admit(conn)==64
        assert conn.total_changes==0
        conn.execute('BEGIN')
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM schema_migrations')


def test_direct_development_import_keeps_source_and_history(development93,tmp_path):
    before = development93.read_bytes()
    contract = destination.for_source(development93)
    output = tmp_path/'rebuilt.db'
    report = build.construct(development93,output,contract,batch_size=2)
    assert report['destination']==contract.version
    assert development93.read_bytes()==before
    store = Store(output)
    try:
        store.init_schema()
        assert runner.history(store.conn,registry.MIGRATIONS)==107
        assert store.conn.execute('SELECT max(version) FROM database_rebuild_source_history').fetchone()[0]==93
        assert [tuple(row) for row in store.conn.execute('SELECT id,label FROM accounts ORDER BY id')]==[
            (11,'Main'),(29,'Other account')]
        assert [tuple(row) for row in store.conn.execute('SELECT id,account_id,recording_id,membership_ref_id FROM play_events')]==[
            (91,11,8,None)]
        assert [row[0] for row in store.conn.execute('SELECT video_id FROM playlist_items WHERE playlist_id=41 ORDER BY position')]==[
            'video-19','video-3','video-8']
        store.settings.set_setting('post_rebuild_write','works')
        assert store.settings.get_setting('post_rebuild_write')=='works'
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_import_refuses_an_unclassified_owner(development93):
    with closing(sqlite3.connect(development93)) as conn:
        conn.execute('CREATE TABLE unique_paid_result(id INTEGER PRIMARY KEY,body BLOB)')
        conn.commit()
    with pytest.raises(ValueError,match='Unclassified source owners: unique_paid_result'):
        destination.for_source(development93)
