"""A populated legacy genre survives real upgrades and remains an explicit choice."""
from contextlib import closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.core.test_catalog import complete_regular_input
from tests.metadata_provenance_support import metadata_provenance
from tests.test_enrich_web import _keep_current_value
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.store import Store


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('legacy-conflict-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def legacy_input(tmp_path_factory):
    path = tmp_path_factory.mktemp('legacy-conflict') / 'library.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        runner.migrate(conn, registry=registry.MIGRATIONS[:7])
        with conn:
            conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key,genre) "
                         "VALUES ('v','Song','Artist','song|artist','My own genre')")
        yield conn, path


def test_legacy_input_keeps_a_populated_value_without_a_provider(legacy_input):
    conn, _ = legacy_input
    runner.validate_version(conn, 7)
    assert tuple(conn.execute('SELECT video_id,genre FROM tracks').fetchone()) == ('v', 'My own genre')


def test_account_reference_retains_metadata_selection_contracts(legacy_input):
    # Compile the independent reference before timing the real rename edge.
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions
    assert 'recording_metadata_selections' in definitions


@pytest.mark.parametrize('target', [8, 20, 63, 79, 80, 88, 94, 102, 103, 107])
def test_each_registered_upgrade_preserves_the_populated_unattributed_genre(legacy_input, target):
    conn, _ = legacy_input
    # One real journey, divided at independently diagnosable storage contracts.
    # A targeted invocation still executes every required predecessor.
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert tuple(conn.execute('SELECT video_id,genre FROM catalog_tracks').fetchone()) == ('v', 'My own genre')
    if 20 <= target < 80:
        assert conn.execute("SELECT source FROM recording_field_sources WHERE field='genre'").fetchone()[0] == 'legacy_unknown'
        assert not conn.execute("SELECT 1 FROM entry_field_choices WHERE field='genre'").fetchone()
    if target >= 80:
        assert tuple(conn.execute("SELECT attribution_status,legacy_source,observation_id "
            "FROM recording_metadata_selections WHERE field='genre'").fetchone()) == (
                'legacy_unresolved', 'legacy_unknown', None)
        assert not conn.execute("SELECT 1 FROM metadata_choice_heads WHERE field='genre'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def regular_input(legacy_input):
    conn, path = legacy_input
    runner.migrate(conn, registry=registry.MIGRATIONS)
    # Supply completed lower regular changes in one transaction; this journey
    # owns numbered conversions and ordinary admission, not regular batching.
    complete_regular_input(conn)
    return conn, path


def test_regular_sql_admission_preserves_legacy_selection_and_history(regular_input):
    conn, _ = regular_input
    before = conn.total_changes
    regular.migrate(conn)
    regular.validate(conn)
    assert conn.total_changes == before
    assert conn.execute('SELECT genre FROM catalog_tracks').fetchone()[0] == 'My own genre'
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 107


@pytest.fixture(scope='module')
def upgraded_store(regular_input):
    _, path = regular_input
    store = Store(path)
    try:
        store.init_schema()
        yield store
    finally:
        store.conn.close()


def test_current_admission_retains_unknown_attribution_without_inventing_an_override(upgraded_store):
    tid = upgraded_store.conn.execute("SELECT id FROM catalog_tracks WHERE video_id='v'").fetchone()[0]
    assert metadata_provenance(upgraded_store, tid)['fields']['genre'] == {
        'value': 'My own genre', 'override': False, 'source': 'legacy_unknown'}
    assert upgraded_store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert upgraded_store._snapshot_revisions


def test_conflict_review_can_keep_legacy_value_outside_provider_candidates(upgraded_store):
    tid = upgraded_store.conn.execute("SELECT id FROM catalog_tracks WHERE video_id='v'").fetchone()[0]
    _keep_current_value(upgraded_store, tid, user_choice=False)
