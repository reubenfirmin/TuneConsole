"""One populated upgrade retains legacy policy and independent account curation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import legacy_evidence, schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These curation owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('curation-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


def history(conn):
    return tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


@pytest.fixture(scope='module')
def predecessor():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        runner.migrate(conn, registry=registry.MIGRATIONS[:18], clock=lambda: 1.)
        yield conn


def test_policy_predecessor_is_the_actual_v18_contract(predecessor):
    runner.validate_version(predecessor, 18)
    assert not predecessor.execute("SELECT 1 FROM sqlite_schema WHERE name='playlist_provenance'").fetchone()


@pytest.fixture(scope='module')
def installed(predecessor, tmp_path_factory):
    path = tmp_path_factory.mktemp('curation-upgrade') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        predecessor.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            conn.execute("INSERT INTO playlist_group VALUES ('gen','Generated')")
            conn.execute("INSERT INTO playlist_group VALUES ('adopted','Faves')")
            conn.execute("INSERT INTO rec_recipes(playlist_ytm,recipe,created_at) VALUES ('adopted','{}',1)")
        yield {'conn': conn, 'path': path, 'numbered': history(conn)}


def test_populated_v18_keeps_the_original_generated_and_adopted_evidence(installed):
    conn = installed['conn']
    assert dict(conn.execute('SELECT * FROM playlist_group')) == {'gen': 'Generated', 'adopted': 'Faves'}
    assert tuple(conn.execute('SELECT * FROM rec_recipes').fetchone()) == ('adopted', '{}', 1)


def assert_retained(state):
    conn = state['conn']
    assert history(conn)[:18] == state['numbered']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', range(19, 103))
def test_real_registered_edges_keep_policy_and_account_scope_custody(installed, target):
    conn = installed['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert history(conn)[-1][0] == target
    if target == 23:
        # The second scenario starts at its actual installed v23 boundary.
        conn.executescript((Path(__file__).parents[1] /
                            'fixtures/migrations/accounts_v23.sql').read_text())
        installed['v23_history'] = history(conn)
        runner.validate_version(conn, 23)
    if target >= 23:
        assert history(conn)[:23] == installed['v23_history']
        assert conn.execute('SELECT count(*) FROM playlists').fetchone()[0] == 5
    if target == 29:
        # The v29 entry has exactly the same retained source, before the
        # actual v30 preservation repair. Neither origin needs another replay.
        runner.validate_version(conn, 29)
        installed['v29_history'] = history(conn)
        # Both installed album states need the same original account cohort:
        # one still has combined membership; the other lost it before repair.
        with conn:
            for identity, provider, retained in (
                    (1, 'unknown-retained', True), (2, 'unknown-discarded', False)):
                conn.execute('INSERT INTO albums(id,title,metadata_rank) VALUES (?,?,2)', (identity, 'Legacy'))
                conn.execute("INSERT INTO album_provider_ids VALUES ('youtube',?,?)", (provider, identity))
                conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                    ('album_legacy:saved_albums', provider,
                     json.dumps({'browse_id': provider, 'title': 'Legacy'}),
                     'original album metadata or relationship'))
                if retained:
                    conn.execute("INSERT INTO album_library_memberships(album_id,source) VALUES (?,'legacy_combined')", (identity,))
            conn.execute('INSERT INTO album_library_snapshots VALUES (1,1)')
            conn.execute('UPDATE identities SET active=0 WHERE id=2')
    if target >= 29:
        assert history(conn)[:29] == installed['v29_history']
    assert_retained(installed)


def test_account_cutover_reference_keeps_policy_and_scope_owners(installed):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions and 'CREATE TABLE playlist_scopes' in definitions


@pytest.fixture(scope='module')
def renamed(installed):
    conn = installed['conn']
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    installed['programs'] = restore.call_args
    return installed


def test_real_account_renaming_retains_memberships_and_exact_prior_history(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_retained(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_real_program_restoration_preserves_scope_references(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_retained(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_owners_keep_curation_and_original_history(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_retained(restored)


@pytest.fixture(scope='module')
def regular_input(restored):
    complete_regular_input(restored['conn'])
    return restored


def test_current_admission_preserves_all_completed_ledgers_and_source_rows(regular_input):
    conn = regular_input['conn']
    before = list(conn.iterdump())
    assert runner.migrate(conn, defer_validation=True) is False
    assert list(conn.iterdump()) == before
    regular.validate(conn)
    assert_retained(regular_input)


@pytest.fixture(scope='module')
def writable(regular_input):
    store = Store(regular_input['path'])
    try:
        store.init_schema()
        yield regular_input, store
    finally:
        store.conn.close()


def test_actual_current_readers_retain_inherited_independent_and_unknown_curation(writable):
    _, store = writable
    assert store.playlists.get_playlist(1).playlist_scope_id == '@account:1:LM'
    assert store.playlists.get_playlist(2).playlist_scope_id == '@account:2:LM'
    assert store.playlists.get_playlist(4).playlist_scope_id == store.playlists.get_playlist(5).playlist_scope_id == 'shared'
    refs = {'@account:1:LM', '@account:2:LM'}
    assert store.playlists.get_playlist_groups() == dict.fromkeys(refs, 'Old likes') | {
        'shared': 'Keep', 'gen': 'Generated', 'adopted': 'Faves'}
    assert store.playlists.get_hidden_playlists() == refs | {'@account:1:SE'}
    assert store.overlaps.get_overlap_ignored() == refs | {'shared'}
    assert store.overlaps.get_cleanup_ignored() == {'empty': refs, 'tiny': {'shared'}}
    assert store.overlaps.get_suppressed_overlap_pairs() == {frozenset(('@legacy:LM', 'shared'))}
    assert store.overlaps.get_ignored_merges()[0]['members'] == ['@account:1:SE', 'shared']
    assert store.playlist_recipes.get_recipe('shared') == {'model': 'cluster'}
    assert json.loads(store.playlist_recipes.get_cluster_canvas('shared')) == {'nodes': []}


def test_file_owned_inheritance_evidence_keeps_the_exact_original_unscoped_choice(writable):
    _, store = writable
    kind = 'playlist_scope_inherited:playlist_group'
    inherited = [json.loads(legacy_evidence.original(store.conn, 'playlist', kind, row[0]))
        for row in store.conn.execute('SELECT source_id FROM playlist_legacy_evidence WHERE kind=?', (kind,))]
    assert {row['inherited']['ytm'] for row in inherited} == {'@account:1:LM', '@account:2:LM'}
    assert all(row['original'] == {'ytm': '@legacy:LM', 'name': 'Old likes'} for row in inherited)


def test_real_policy_reads_and_curation_writes_remain_idempotent(writable):
    state, store = writable
    assert store.playlists.is_quarantined_playlist('gen')
    assert store.playlists.playlist_provenance('adopted')['adopted'] == 1
    store.playlists.set_playlist_group('gen', 'Road trips')
    before = list(store.conn.iterdump())
    store.init_schema()
    assert list(store.conn.iterdump()) == before
    assert store.playlists.is_quarantined_playlist('gen')
    assert store.playlists.get_playlist_groups()['gen'] == 'Road trips'
    assert not (state['path'].parent / 'backups').exists()


def test_fresh_collection_reopen_keeps_both_legacy_states_until_the_inactive_original_owner_observes(writable):
    state, _ = writable
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        fresh.collection.replace_saved_album_snapshots({1: []}, now=2)
        expected = {'unknown-retained', 'unknown-discarded'}
        assert fresh.collection.saved_album_ids() == expected
        assert {row[0] for row in fresh.conn.execute(
            'SELECT account_id FROM account_album_state WHERE legacy_cohort_present=1')} == {1, 2}
        # An inactive account cannot count as a successful empty observation.
        assert fresh.collection.replace_saved_album_snapshots({2: []}, now=3) == set()
        assert fresh.collection.saved_album_ids() == expected
        with fresh.conn:
            fresh.conn.execute('UPDATE accounts SET active=1 WHERE id=2')
        fresh.collection.replace_saved_album_snapshots({2: []}, now=4)
        assert fresh.collection.saved_album_ids() == set()
        assert not fresh.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        fresh.conn.close()


def test_real_membership_churn_preserves_independent_choices_and_gives_new_accounts_no_inheritance(writable):
    _, store = writable
    store.playlists.set_playlist_group('LM', 'Changed', account_id=1)
    store.playlists.unhide_playlist('LM', account_id=1)
    store.overlaps.unignore_cleanup('@account:1:LM', 'empty')
    store.playlists.remove_playlist(2)
    restored = store.playlists.upsert_playlist(2, 'LM', 'Main likes', 0, 'h', 1)
    third = store.accounts.upsert_account('New', 'bridge', 'new-brand', False)
    new = store.playlists.upsert_playlist(third, 'LM', 'New likes', 0, 'h', 1)
    assert store.playlists.get_playlist_groups()['@account:1:LM'] == 'Changed'
    assert store.playlists.get_playlist_groups()[store.playlists.get_playlist(restored).playlist_scope_id] == 'Old likes'
    assert store.playlists.get_playlist(new).playlist_scope_id not in store.playlists.get_playlist_groups()
    assert store.playlists.get_hidden_playlists() == {'@account:2:LM', '@account:1:SE'}
    assert store.overlaps.get_cleanup_ignored()['empty'] == {'@account:2:LM'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_fresh_store_reopen_keeps_all_post_upgrade_curation_and_policy_bytes(writable):
    state, store = writable
    before = list(store.conn.iterdump())
    store.conn.close()
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        assert list(fresh.conn.iterdump()) == before
        assert fresh.playlists.is_quarantined_playlist('gen')
        assert fresh.playlists.playlist_provenance('adopted')['adopted'] == 1
        assert fresh.playlists.get_playlist_groups()['gen'] == 'Road trips'
        assert fresh.playlists.get_playlist_groups()['@account:1:LM'] == 'Changed'
        assert not fresh.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        fresh.conn.close()
