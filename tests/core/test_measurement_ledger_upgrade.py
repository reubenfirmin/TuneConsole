"""Keep genuine v8 impressions while later ledgers become writable and reopen."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store
from yt_playlist.rec import measurement, scorecard


LEGACY_COLUMNS = 'id,request_id,surface,lane,identity_key,rank,model_version,provenance,served_at'
UPGRADE_EDGES = (20, 40, 60, 65, *range(66, 103))


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """No HTTP application is needed by these migration/reporting owners."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('measurement-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def predecessor():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        runner.migrate(conn, registry=registry.MIGRATIONS[:8], clock=lambda: 1.)
        yield conn


def test_measurement_predecessor_is_the_actual_v8_contract(predecessor):
    runner.validate_version(predecessor, 8)
    assert predecessor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 8
    assert not predecessor.execute("SELECT 1 FROM sqlite_schema WHERE name='rec_serving_policies'").fetchone()


def history(conn):
    return tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


@pytest.fixture(scope='module')
def upgrade(predecessor, tmp_path_factory):
    path = tmp_path_factory.mktemp('measurement-upgrade') / 'measurement.db'
    with closing(sqlite3.connect(path)) as conn:
        predecessor.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            conn.execute('INSERT INTO rec_served_impressions '
                '(request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) '
                'VALUES (?,?,?,?,?,?,?,?)', ('legacy', 'home', '', 'old|artist', 1, 'legacy', '{}', 50))
        original = tuple(conn.execute(f'SELECT {LEGACY_COLUMNS} FROM rec_served_impressions').fetchone())
        yield {'conn': conn, 'path': path, 'history': history(conn), 'original': original}


def assert_legacy(state):
    conn = state['conn']
    assert history(conn)[:8] == state['history']
    assert tuple(conn.execute(
        f'SELECT {LEGACY_COLUMNS} FROM rec_served_impressions WHERE id=1').fetchone()) == state['original']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_populated_v8_input_keeps_exact_unknown_ownership_and_impression_bytes(upgrade):
    assert_legacy(upgrade)
    assert upgrade['original'] == (1, 'legacy', 'home', '', 'old|artist', 1, 'legacy', '{}', 50)


@pytest.mark.parametrize('target', UPGRADE_EDGES)
def test_real_registered_owners_keep_original_impression_and_applied_history(upgrade, target):
    runner.migrate(upgrade['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert history(upgrade['conn'])[-1][0] == target
    assert_legacy(upgrade)


def test_account_cutover_reference_retains_impression_ownership(upgrade):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions and 'rec_served_impressions' in definitions


@pytest.fixture(scope='module')
def renamed(upgrade):
    conn = upgrade['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:102])
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    upgrade['programs'] = restore.call_args
    return upgrade


def test_actual_account_renaming_keeps_the_old_impression_and_foreign_keys(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_legacy(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_real_program_restoration_keeps_impressions_and_current_constraints(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_legacy(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_current_registered_owners_keep_old_impressions_without_rewriting_history(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_legacy(restored)


@pytest.fixture(scope='module')
def regular_input(restored):
    complete_regular_input(restored['conn'])
    return restored


def test_current_admission_keeps_old_impressions_and_completed_migration_ledgers(regular_input):
    conn = regular_input['conn']
    before = list(conn.iterdump())
    assert runner.migrate(conn, defer_validation=True) is False
    assert list(conn.iterdump()) == before
    regular.validate(conn)
    assert_legacy(regular_input)


@pytest.fixture(scope='module')
def admitted(regular_input):
    store = Store(regular_input['path'])
    try:
        store.init_schema()
        yield regular_input, store
    finally:
        store.conn.close()


def test_new_recommendation_and_save_are_written_through_the_actual_current_controllers(admitted):
    state, store = admitted
    assert history(store.conn)[:8] == state['history']
    assert tuple(store.conn.execute(
        f'SELECT {LEGACY_COLUMNS} FROM rec_served_impressions WHERE id=1').fetchone()) == state['original']
    measurement.record_recommendations(store, 'home', [{'key': 'new|artist', 'score': .4}], 100)
    store.rec_ledger.record_recommendation_outcomes('save', 'save', ['new|artist'], 110)
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 2
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def reopened(admitted):
    state, store = admitted
    store.conn.close()
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        yield state, fresh
    finally:
        fresh.conn.close()


def test_fresh_reopen_reports_old_and_new_impressions_and_saved_outcome(reopened):
    state, store = reopened
    assert history(store.conn)[:8] == state['history']
    assert tuple(store.conn.execute(
        f'SELECT {LEGACY_COLUMNS} FROM rec_served_impressions WHERE id=1').fetchone()) == state['original']
    score = scorecard.recommendation_scorecard(store, 40, 105)
    assert score['impressions'] == 2 and score['save'] == 1
    assert score['by_ownership']['unknown']['impressions'] == 2
    trace = scorecard.served_recommendation(store, 2)
    assert trace['configuration']['algorithm'] == 'served-ranking-v2'


def test_continuing_recommendation_and_save_use_the_same_reopened_ledger(reopened):
    _, store = reopened
    measurement.record_recommendations(store, 'home', [{'key': 'later|artist', 'score': .7}], 120)
    store.rec_ledger.record_recommendation_outcomes('save', 'save', ['later|artist'], 130)
    score = scorecard.recommendation_scorecard(store, 115, 125)
    assert score['impressions'] == 1 and score['save'] == 1
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
