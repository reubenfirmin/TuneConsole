"""Opening a read must leave its budget available for useful library work."""
import sqlite3
from contextlib import closing

import pytest

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.db_reads import MAX_READERS
from yt_playlist.core.store import Store


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
        assert reader._collection_files == store._collection_files
        assert reader._compact_group_files == store._compact_group_files
        assert reader._legacy_files == store._legacy_files
    assert steps < 20000, 'Repeated catalog scans leave no time for the actual read'


def test_new_readers_detect_committed_feature_tables_and_ignore_views(tmp_path):
    store = Store(tmp_path / 'feature-tables.db')
    try:
        store.conn.execute('PRAGMA journal_mode=WAL')
        store.conn.execute('CREATE TABLE probe(value)')
        store.conn.execute('CREATE VIEW album_feedback AS SELECT value FROM probe')
        store.conn.commit()
        store._bind_repositories()
        with store.read_view() as before:
            assert not before._collection_files
            assert not before._legacy_files
            with closing(sqlite3.connect(store._db_path)) as writer, writer:
                writer.execute('DROP VIEW album_feedback')
                writer.execute('CREATE TABLE album_feedback(value)')
                writer.execute('CREATE TABLE legacy_catalog_evidence(value)')
            # An existing reader keeps its pinned schema and feature flags.
            assert not before._collection_files and not before._legacy_files
        with store.read_view() as after:
            assert after._collection_files and after._legacy_files
    finally:
        store.conn.close()


def test_new_reader_detects_replacement_with_same_schema_version(tmp_path):
    database = tmp_path / 'replaced.db'
    store = Store(database)
    try:
        store.conn.execute('CREATE TABLE original(value)')
        store.conn.commit()
        store._bind_repositories()
        version = store.conn.execute('PRAGMA schema_version').fetchone()[0]
        with store.read_view() as before:
            assert not before._legacy_files
        store.conn.close()
        replacement = tmp_path / 'replacement.db'
        with closing(sqlite3.connect(replacement)) as writer, writer:
            writer.execute('CREATE TABLE legacy_catalog_evidence(value)')
            writer.execute(f'PRAGMA schema_version={version}')
        replacement.replace(database)
        # The owner still carries the old flags and path. A new connection
        # must derive its layout from the database it actually opened.
        assert not store._legacy_files
        with store.read_view() as after:
            assert after._repository_schema_version == version
            assert after._legacy_files
    finally:
        store.conn.close()


@pytest.mark.parametrize('version', [79, 94, 107])
def test_supported_installed_layouts_keep_private_read_projections(
        tmp_path, _pristine_installed_schema, version):
    database = tmp_path / f'installed-{version}.db'
    with closing(sqlite3.connect(database)) as copied:
        _pristine_installed_schema(version).backup(copied)
    store = Store(database)
    try:
        store.init_schema(defer_metadata=True)
        store.settings.set_setting('reader-layout-probe', 'current')
        store.tracks.upsert_track('reader-layout-video', 'Reader layout', 'Artist', None, None)
        with store.read_view() as reader:
            for name in ('_collection_files', '_compact_group_files', '_recommendation_files',
                         '_metadata_files', '_legacy_files'):
                assert getattr(reader, name) == getattr(store, name)
            assert reader.settings.get_setting('reader-layout-probe') == 'current'
            assert reader.conn.execute('SELECT title FROM tracks WHERE video_id=?',
                                       ('reader-layout-video',)).fetchone()[0] == 'Reader layout'
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
