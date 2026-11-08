"""Actual genre cutover keeps aliases, unresolved text and current display reads."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import albums, genre_classifications, schema_definition
from yt_playlist.core.migrations import current, registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store
from yt_playlist.util import genre_catalog


LABELS = ['Drum And Bass', 'Synth Pop', 'Fix Artist Credits', '', None]


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These historical SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('classification-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch, entries=(('drum bass', 'Drum & Bass'), ('synth pop', 'Synth Pop')))
        records, labels = genre_catalog.catalog()
        labels['drum and bass'] = ('drum bass', 'Drum And Bass', 'curated')
        patch.setattr(genre_catalog, 'catalog', lambda: (records, labels))
        yield


@pytest.fixture(scope='module')
def predecessor(tmp_path_factory):
    path = tmp_path_factory.mktemp('classification-upgrade') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        install_numbered_input(conn, 85)
        yield {'conn': conn, 'path': path,
               'numbered': tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))}


def test_predecessor_is_the_actual_supported_v85_contract(predecessor):
    runner.validate_version(predecessor['conn'], 85)
    assert predecessor['conn'].execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.fixture(scope='module')
def populated(predecessor):
    conn = predecessor['conn']
    with conn:
        conn.executemany('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
                         [(i + 1, 'recording:' + str(i), label) for i, label in enumerate(LABELS)])
        predecessor['album'] = albums.observe(conn, 'release', title='Release', artist='Artist', genre=LABELS[0])
    with catalog_reader(predecessor['path'], 85) as store:
        store.discovery.upsert_discovered_artist('New artist', 1, [], [], None, 10, genre=LABELS[0])
    return predecessor


def test_source_keeps_all_five_literal_labels_before_the_actual_cutover(populated):
    assert [row[0] for row in populated['conn'].execute('SELECT genre FROM recordings ORDER BY id')] == LABELS


def assert_classifications(state):
    conn = state['conn']
    assert [row[0] for row in conn.execute('SELECT genre_key FROM recordings ORDER BY id')] == [
        'drum bass', 'synth pop', None, None, None]
    assert [row[0] for row in conn.execute('SELECT unresolved_genre FROM recordings ORDER BY id')] == [
        None, None, 'Fix Artist Credits', '', None]
    assert [row[0] for row in conn.execute('SELECT raw_label FROM genre_classification_legacy '
        'WHERE recording_id IS NOT NULL ORDER BY recording_id')] == LABELS[:-1]
    assert genre_classifications.read(conn, 'albums', state['album']) == 'Drum & Bass'
    assert tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:85] == state['numbered']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', range(86, 103))
def test_real_registered_upgrades_keep_classifications_and_original_raw_labels(populated, target):
    runner.migrate(populated['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_classifications(populated)


def test_account_cutover_reference_retains_the_same_genre_owners(populated):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions and 'genre_classification_legacy' in definitions


@pytest.fixture(scope='module')
def renamed(populated):
    conn = populated['conn']
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    populated['programs'] = restore.call_args
    return populated


def test_real_account_renaming_keeps_genre_references_and_source_labels(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_classifications(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_real_program_restoration_keeps_current_genre_constraints(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_classifications(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_owners_keep_every_genre_classification(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_classifications(restored)


@pytest.fixture(scope='module')
def regular_input(restored):
    complete_regular_input(restored['conn'])
    return restored


def test_real_current_admission_retains_original_numbered_history_and_genre_evidence(regular_input):
    conn = regular_input['conn']
    before = list(conn.iterdump())
    assert runner.migrate(conn, defer_validation=True) is False
    assert list(conn.iterdump()) == before
    assert_classifications(regular_input)
    regular.validate(conn)


@pytest.fixture(scope='module')
def writable(regular_input):
    store = Store(regular_input['path'])
    try:
        store.init_schema()
        yield regular_input, store
    finally:
        store.conn.close()


def test_actual_current_consumers_follow_canonical_renames_without_rewriting_evidence(writable):
    state, store = writable
    assert store.discovery.get_discovered_artists()[0]['genre'] == 'Drum & Bass'
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET name='Drums renamed' WHERE key='drum bass'")
    assert genre_classifications.read(store.conn, 'recordings', 1) == 'Drums renamed'
    assert genre_classifications.read(store.conn, 'albums', state['album']) == 'Drums renamed'
    assert store.discovery.get_discovered_artists()[0]['genre'] == 'Drums renamed'
    assert store.conn.execute('SELECT raw_label FROM genre_classification_legacy WHERE recording_id=1').fetchone()[0] == LABELS[0]
    current.validate(store.conn)


def test_fresh_store_reopen_keeps_renamed_display_and_exact_original_label(writable):
    state, store = writable
    before = list(store.conn.iterdump())
    store.conn.close()
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        assert list(fresh.conn.iterdump()) == before
        assert genre_classifications.read(fresh.conn, 'recordings', 1) == 'Drums renamed'
        assert fresh.conn.execute('SELECT raw_label FROM genre_classification_legacy WHERE recording_id=1').fetchone()[0] == LABELS[0]
        assert not fresh.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        fresh.conn.close()
