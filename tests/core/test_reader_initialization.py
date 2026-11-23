"""Opening a read must leave its budget available for useful library work."""
import sqlite3
from contextlib import closing

import pytest

from yt_playlist.core import metadata_runtime_files
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.db_reads import MAX_READERS
from yt_playlist.core.store import Store
from yt_playlist.repos.collection_recovery import CollectionRecovery


def deny_schema_access(action, first, second, database, source):
    if action == sqlite3.SQLITE_READ and first in {'sqlite_schema', 'sqlite_master', 'sqlite_temp_master'}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and first in {'table_info', 'table_xinfo', 'table_list', 'schema_version'}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def test_reader_setup_work_is_bounded_on_a_large_schema(file_backed_store, monkeypatch):
    store = file_backed_store
    with store.conn:
        for n in range(600):
            store.conn.execute(f'CREATE TABLE reader_probe_{n}(value)')
    configure = Store._configure_connection
    steps = 0

    def counted_configuration(reader):
        configure(reader)
        # SQLite loads/parses schema definitions on the first catalog access.
        # Measure catalog probing separately from that unavoidable one-time load.
        reader.conn.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone()

        def progress():
            nonlocal steps
            steps += 100
            return int(steps > 20000)

        reader.conn.set_progress_handler(progress, 100)

    monkeypatch.setattr(Store, '_configure_connection', counted_configuration)
    with store.read_view() as reader:
        assert reader.conn.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0
    assert steps < 20000, 'Repeated catalog scans leave no time for the actual read'


def test_short_lived_reader_binds_and_reads_without_catalog_queries(file_backed_store, monkeypatch):
    from yt_playlist.rec import musical_character, vibe_intensity
    from yt_playlist.rec.stored_radio import score_patterns

    configure = Store._configure_connection

    def guarded_configuration(reader):
        configure(reader)
        reader.conn.set_authorizer(deny_schema_access)

    monkeypatch.setattr(Store, '_configure_connection', guarded_configuration)
    with file_backed_store.read_view() as reader:
        assert reader.settings.get_setting('reader-layout-probe') is None
        assert reader.rec_model.recording_state().generation is None
        assert list(CollectionRecovery(reader.conn).references()) == []
        path = metadata_runtime_files.reference_path(reader.conn, 'choices', (1,), 1)
        assert path.name == '1.json'
        measurement, joint, _ = score_patterns()
        intensity = (musical_character.VERSION, measurement, joint, vibe_intensity.SOURCES)
        assert reader.radio_catalog.tuning_ranks(1, 'radio', 'membership', intensity=intensity) == []


@pytest.mark.parametrize('prepared', [False, True], ids=['pending-metadata', 'prepared-metadata'])
def test_current_track_writes_choices_and_tuning_use_admitted_owners(file_backed_store, prepared):
    from yt_playlist.core import catalog, metadata, metadata_runtime

    store = file_backed_store
    store.genres.configure_catalog()
    conn = store.conn
    if prepared:
        with conn:
            metadata_runtime.activate(conn)

    conn.set_authorizer(deny_schema_access)
    try:
        old = store.tracks.upsert_track('source', 'Original', 'Artist', None, 200, library=True)
        target = store.tracks.upsert_track('target', 'Target', 'Artist', None, 210, library=True)
        recording = store.tracks.catalog_subject(target)['recording_id']
        store.tracks.associate_youtube_item('source', recording, source='verified',
                                           evidence={'same': True}, now=1, same_recording=True)
        assert store.tracks.catalog_subject(old)['recording_id'] == recording
        assert store.tracks.remove_song_library_membership('source')
        assert not store.tracks.song_library_membership('source')['owned']

        with conn:
            recovered = catalog.upsert(conn, 'recovered', 'Recovered', 'Artist',
                                       album='Release', album_browse_id='release')
            metadata.select(conn, recovered, 'genre', 'Jazz', 'provider', now=2)
        store.metadata_choices.choose(recovered, 'genre', 'Rock', now=3)
        assert metadata.has_choice(conn, recovered, 'genre')
        store.metadata_choices.choose(recovered, 'genre', withdraw=True, now=4)
        assert not metadata.has_choice(conn, recovered, 'genre')
        assert store.tracks.get_track_enrichment(recovered)[0] == 'Jazz'
        assert conn.execute("SELECT value FROM recording_metadata_history WHERE operation='reset' "
                            "AND entry_id=? AND field='genre'", (recovered,)).fetchone()[0] == 'Jazz'
    finally:
        conn.set_authorizer(None)

    with store.read_view() as reader:
        reader.conn.set_authorizer(deny_schema_access)
        ranks = reader.radio_catalog.tuning_ranks(1, 'radio', 'membership', pinned_genre='jazz')
        assert [(row['video_id'], row['genre']) for row in ranks] == [('recovered', 'Jazz')]


@pytest.mark.parametrize('version', [107])
def test_supported_installed_layouts_keep_private_read_projections(
        tmp_path, _pristine_installed_schema, version):
    database = tmp_path / f'installed-{version}.db'
    with closing(sqlite3.connect(database)) as copied:
        _pristine_installed_schema(version).backup(copied)
    store = Store(database)
    try:
        store.init_schema()
        store.settings.set_setting('reader-layout-probe', 'current')
        store.tracks.upsert_track('reader-layout-video', 'Reader layout', 'Artist', None, None)
        with store.read_view() as reader:
            assert not reader.conn.in_transaction
            assert reader.settings.get_setting('reader-layout-probe') == 'current'
            assert reader.conn.execute('SELECT title FROM catalog_tracks WHERE video_id=?',
                                       ('reader-layout-video',)).fetchone()[0] == 'Reader layout'
            assert not reader.conn.in_transaction
    finally:
        store.conn.close()


@pytest.mark.parametrize('interruption', [WorkBudgetExceeded, WorkClosing])
def test_interrupted_reader_setup_closes_handle_and_releases_admission(
        file_backed_store, monkeypatch, interruption):
    store = file_backed_store
    bind = Store._bind_repositories
    opened = []

    def interrupted(reader):
        opened.append(reader.conn)
        bind(reader)
        raise interruption('Interrupted during reader setup')

    with monkeypatch.context() as patch:
        patch.setattr(Store, '_bind_repositories', interrupted)
        with pytest.raises(interruption, match='during reader setup'):
            with store.read_view():
                pytest.fail('Interrupted setup must not expose a reader')
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')
    acquired = 0
    try:
        for _ in range(MAX_READERS):
            assert store._analytics_readers.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            store._analytics_readers.release()
    with store.read_view() as reader:
        assert reader.conn.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0
