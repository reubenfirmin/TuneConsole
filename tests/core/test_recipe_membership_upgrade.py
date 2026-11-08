"""Real ordered membership cutover from a bounded supported predecessor."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core.migrations import current, registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store
from yt_playlist.util import genre_catalog


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('recipe-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch, entries=(('synth pop', 'Synth Pop'),
                                     ('drum bass', 'Drum & Bass'), ('jazz', 'Jazz')))
        records, labels = genre_catalog.catalog()
        labels['drum and bass'] = ('drum bass', 'Drum And Bass', 'curated')
        patch.setattr(genre_catalog, 'catalog', lambda: (records, labels))
        yield


@pytest.fixture(scope='module')
def predecessor(tmp_path_factory):
    path = tmp_path_factory.mktemp('recipe-upgrade') / 'recipes.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        install_numbered_input(conn, 88)
        yield {'conn': conn, 'path': path,
               'history': list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))}


def test_predecessor_is_the_real_supported_v88_contract(predecessor):
    runner.validate_version(predecessor['conn'], 88)
    assert predecessor['conn'].execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.fixture(scope='module')
def populated(predecessor):
    with catalog_reader(predecessor['path'], 88) as store:
        store.tracks.upsert_track('known', 'Song', 'Known artist', None, 120)
        predecessor['recipe'] = store.road_trip.save_road_trip_recipe(
            None, 'Trip', 50, ['Unknown artist', 'Known artist'],
            ['Synth Pop', 'Unverified old label'], 60, 100,
            blacklist_genres=['Drum And Bass'])
        predecessor['legacy'] = store.road_trip.get_road_trip_recipe(predecessor['recipe'])
    predecessor['original'] = tuple(predecessor['conn'].execute(
        'SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=?',
        (predecessor['recipe'],)).fetchone())
    return predecessor


def assert_legacy(state):
    conn = state['conn']
    assert {'artists', 'genres', 'blacklist_genres'} <= {
        row[1] for row in conn.execute('PRAGMA table_info(road_trip_recipes)')}
    assert tuple(conn.execute(
        'SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=?',
        (state['recipe'],)).fetchone()) == state['original']
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == state['history']
    with catalog_reader(state['path'], 88) as store:
        recipe = store.road_trip.get_road_trip_recipe(state['recipe'])
        assert recipe == state['legacy']
        assert recipe['artists'] == ['Unknown artist', 'Known artist']
        assert recipe['genres'] == ['Synth Pop', 'Unverified old label']
        assert recipe['blacklist_genres'] == ['Drum And Bass']


def test_actual_legacy_writer_preserves_literal_order_unknowns_and_blacklist(populated):
    assert_legacy(populated)


@pytest.fixture(scope='module')
def regular_input(populated):
    complete_regular_input(populated['conn'])
    return populated


def test_unrelated_completed_regular_owners_leave_the_legacy_recipe_unchanged(regular_input):
    assert_legacy(regular_input)
    regular.validate(regular_input['conn'])


def test_real_production_admission_keeps_the_supported_legacy_writer(regular_input):
    assert runner.migrate(regular_input['conn']) is True
    assert_legacy(regular_input)
    with catalog_reader(regular_input['path'], 88) as store:
        other = store.road_trip.save_road_trip_recipe(
            None, 'New trip', 50, ['Another unknown'], ['Jazz'], 60, 101)
        assert store.road_trip.get_road_trip_recipe(other)['artists'] == ['Another unknown']
        store.road_trip.save_road_trip_recipe(
            other, 'Changed trip', 60, ['Known artist'], ['Synth Pop'], 90, 102)
        assert store.road_trip.get_road_trip_recipe(other)['genres'] == ['Synth Pop']
        store.road_trip.delete_road_trip_recipe(other)
        assert store.road_trip.get_road_trip_recipe(other) is None
    assert_legacy(regular_input)


def assert_memberships(state):
    conn = state['conn']
    assert 'artists' not in {row[1] for row in conn.execute('PRAGMA table_info(road_trip_recipes)')}
    assert [row[0] for row in conn.execute('SELECT position FROM recipe_artists')] == [1]
    assert list(map(tuple, conn.execute('SELECT raw_label,position FROM recipe_unresolved_artists'))) == [('Unknown artist', 0)]
    assert list(map(tuple, conn.execute('SELECT raw_label,position FROM recipe_unresolved_genres'))) == [('Unverified old label', 1)]
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:88] == state['history']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', range(89, 103))
def test_actual_registered_cutover_and_later_owners_keep_ordered_memberships(regular_input, target):
    runner.migrate(regular_input['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_memberships(regular_input)


@pytest.fixture(scope='module')
def renamed(regular_input):
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(regular_input['conn'], registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(regular_input['conn'])
    regular_input['programs'] = restore.call_args
    return regular_input


def test_actual_account_rename_preserves_order_and_unknown_memberships(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_memberships(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_actual_program_restoration_keeps_the_current_membership_contract(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_memberships(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_owners_keep_original_positions_and_history(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_memberships(restored)


def test_current_admission_preserves_exact_sql_and_numbered_history(restored):
    before = list(restored['conn'].iterdump())
    assert runner.migrate(restored['conn']) is False
    assert list(restored['conn'].iterdump()) == before
    assert_memberships(restored)


@pytest.fixture(scope='module')
def writable(restored):
    with catalog_reader(restored['path'], len(registry.MIGRATIONS)) as store:
        yield restored, store


def test_real_genre_identity_rename_and_merge_update_display_without_losing_unknowns(writable):
    state, store = writable
    assert store.road_trip.get_road_trip_recipe(state['recipe'])['artists'] == ['Unknown artist', 'Known artist']
    store.genres.rename_genre('synth pop', 'A renamed genre')
    assert store.road_trip.get_road_trip_recipe(state['recipe'])['genres'] == ['A renamed genre', 'Unverified old label']
    store.genres.merge_genres('synth pop', 'jazz', now=200)
    assert store.road_trip.get_road_trip_recipe(state['recipe'])['genres'] == ['Jazz', 'Unverified old label']
    current.validate(store.conn)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_real_fresh_store_reopen_preserves_order_display_blacklist_and_exact_database(writable):
    state, store = writable
    before = list(store.conn.iterdump())
    store.conn.close()
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        assert list(fresh.conn.iterdump()) == before
        recipe = fresh.road_trip.get_road_trip_recipe(state['recipe'])
        assert recipe['artists'] == ['Unknown artist', 'Known artist']
        assert recipe['genres'] == ['Jazz', 'Unverified old label']
        assert recipe['blacklist_genres'] == ['Drum & Bass']
        current.validate(fresh.conn)
        assert not fresh.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        fresh.conn.close()
