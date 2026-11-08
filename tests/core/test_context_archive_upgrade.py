"""Real context publications retain exact originals through registered upgrades."""
from contextlib import closing
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core import test_discovery_checkpoint_publication as checkpoints
from tests.core import test_library_sync_publication as sync
from tests.core import test_playback_publication as playback
from tests.core import test_radio_origin_publication as origins
from tests.core import test_trial_publication as trials
from tests.genre_catalog_support import bound_catalog
from tests.core.test_catalog import complete_regular_input
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These owners exercise SQL and files without starting an HTTP app."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('context-archive-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def context_numbered():
    with closing(sqlite3.connect(':memory:')) as schema:
        install_numbered_input(schema, 93)
        yield schema


def test_context_predecessor_keeps_the_literal_v93_contract(context_numbered):
    runner.validate_version(context_numbered, 93)
    regular.validate(context_numbered)
    assert context_numbered.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93


@pytest.fixture(scope='module')
def context_admitted(context_numbered, tmp_path_factory):
    path = tmp_path_factory.mktemp('context-admitted') / 'input.db'
    with prepared_template(context_numbered, lambda store: None, database=path, defer_metadata=True) as template:
        yield template


def test_context_predecessor_admits_real_wal_and_revision_guards(context_admitted):
    source = context_admitted.source
    assert source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert context_admitted.revision_guards
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


@pytest.fixture(scope='module')
def context_seeded(context_admitted, tmp_path_factory):
    with context_admitted.open(tmp_path_factory.mktemp('context-source') / 'source.db') as store:
        # All five producers share the actual First/Second identities and Song.
        # Complete every continuing write before selecting any archive.
        sync_value = json.loads(sync.seed(store))
        del sync_value['accounts']['999']
        del sync_value['accounts']['1']['resources']['playlists']['items']['missing-playlist']
        sync_raw = json.dumps(sync_value)
        store.settings.set_setting(sync.sync_state.KEY, sync_raw)
        checkpoints.seed(store)
        playback_originals = playback.seed(store)
        bindings = playback.preparation.BINDINGS_KEY
        playback_originals[bindings] = json.dumps(json.loads(playback_originals[bindings])[:2])
        store.settings.set_setting(bindings, playback_originals[bindings])
        origin_originals = origins.seed(store)
        trial_values, trial_originals = trials.seed(store)
        # Frozen converters require admitted references. Separate compatibility
        # owners retain the missing-reference and noncanonical spelling cases.
        with store.conn:
            for name, suffix in (('fetch-history', 'unknown-release'), ('artist-images', '01'),
                                 ('artist-images', '999'), ('provider-refresh', 'unknown')):
                store.conn.execute('DELETE FROM settings WHERE key=?',
                    (checkpoints.preparation.PREFIXES[name] + suffix,))
        expected = dict(sync_value=sync_value, sync_raw=sync_raw,
                        playback_originals=playback_originals, origin_originals=origin_originals,
                        trial_values=trial_values, trial_originals=trial_originals,
                        trial_revisions={}, checkpoint_rows={}, published=[])
        yield store, expected


def test_context_sources_keep_all_five_original_domains_before_selection(context_seeded):
    store, expected = context_seeded
    assert sync.sync_state.read(store.conn) == expected['sync_value']
    assert all(store.settings.get_setting(source) == raw
               for source, raw in expected['playback_originals'].items())
    assert all(store.settings.get_setting(source) == raw
               for source, raw in expected['origin_originals'].items())
    assert set(expected['trial_values']) == set(expected['trial_originals'])


def publish_context(store, expected, domain):
    if domain in expected['published']:
        return
    if domain == 'sync':
        for owner in sync.publication.OWNERS.values():
            owner.publish(store.conn, revision=sync.prepare(store.conn, owner))
            assert owner.selected(store.conn) == 0
    elif domain == 'playback':
        for owner in playback.publication.OWNERS.values():
            owner.publish(store.conn, revision=playback.prepare(store.conn, owner))
            assert owner.selected(store.conn) == 0
        expected['playback'] = playback.album_playback_context.load(store.conn)
    elif domain == 'origins':
        origins.publication.publish(store.conn, revision=origins.prepare(store.conn))
        assert origins.publication.selected(store.conn) == 0
        expected['origin_heads'] = {source: dict(origins.head(store.conn, source))
                                   for source in expected['origin_originals']}
    elif domain == 'trials':
        for name, owner in trials.publication.OWNERS.items():
            revision = trials.ready(owner, store.conn)
            owner.publish(store.conn, revision=revision)
            expected['trial_revisions'][name] = revision
            assert owner.selected(store.conn) == revision
    else:
        with checkpoints.connect(store) as job:
            for name, owner in checkpoints.publication.OWNERS.items():
                identity = checkpoints.ready(job, name)
                owner.publish(job, stage=identity)
                assert owner.selected(job) == identity
                expected['checkpoint_rows'][name] = list(map(tuple, job.execute(
                    'SELECT * FROM ' + owner.table('rows') + ' ORDER BY source')))
    expected['published'].append(domain)


@pytest.mark.parametrize('domain', ['sync', 'playback', 'origins', 'trials', 'checkpoints'])
def test_each_context_publication_selects_its_actual_typed_rows_and_original_files(context_seeded, domain):
    store, expected = context_seeded
    publish_context(store, expected, domain)
    assert domain in expected['published']


@pytest.fixture(scope='module')
def context_published(context_seeded):
    store, expected = context_seeded
    for domain in ('sync', 'playback', 'origins', 'trials', 'checkpoints'):
        publish_context(store, expected, domain)
    # These are observable preconditions, not a completed-publication mock.
    for owner in (*sync.publication.OWNERS.values(), *playback.publication.OWNERS.values()):
        assert owner.selected(store.conn) == 0
    assert origins.publication.selected(store.conn) == 0
    assert set(expected['trial_revisions']) == set(trials.publication.OWNERS)
    assert set(expected['checkpoint_rows']) == set(checkpoints.publication.OWNERS)
    return capture_template(store, expected)


def test_all_selected_context_archives_are_frozen_with_real_file_custody(context_published):
    assert context_published.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert context_published.coordinates
    assert any(root.exists() for root in context_published.files.values())


@pytest.fixture(scope='module')
def context_upgrade(context_published, tmp_path_factory):
    path = tmp_path_factory.mktemp('context-native') / 'native.db'
    with context_published.open(path) as store, closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn, path, context_published.result


def test_context_upgrade_copy_keeps_private_files_wal_and_every_selected_owner(context_upgrade, context_published):
    conn, path, _ = context_upgrade
    assert path != context_published.source._db_path
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    for owner in (*sync.publication.OWNERS.values(), *playback.publication.OWNERS.values()):
        assert owner.selected(conn) == 0


def assert_native_context(conn, expected, *, trials_native):
    assert sync.publication.RESULT.selected(conn) is None
    assert sync.publication.STAMPS.selected(conn) is None
    assert sync.sync_state.read(conn) == expected['sync_value']
    assert sync.sync_state.stamp(conn, 'last_sync_at') == '10.5'
    assert sync.publication.RESULT.value(conn, 0) == expected['sync_raw']
    assert sync.publication.STAMPS.value(conn, 'last_sync_at', 0) == ' 10.50 '
    for owner in playback.publication.OWNERS.values():
        assert owner.selected(conn) is None
        assert owner.value(conn, 0) == expected['playback_originals'][owner.source]
    assert playback.album_playback_context.load(conn) == expected['playback']
    assert playback.playback_bindings.find(conn, 1, 'LM', 'video', 13, 100)['opportunity_id'] == 'later-menu'
    assert origins.publication.selected(conn) is None
    assert origins.radio_provenance.ids(conn) == ['LM', 'missing', 'shared']
    for source, raw in expected['origin_originals'].items():
        assert origins.preparation.read_original(conn, expected['origin_heads'][source]) == raw
    for name, owner in checkpoints.publication.OWNERS.items():
        assert owner.selected(conn) is None
        assert list(map(tuple, conn.execute('SELECT * FROM ' + owner.table('rows') + ' ORDER BY source'))) == expected['checkpoint_rows'][name]
    assert checkpoints.provider_frontiers.read(conn, 'known')['fingerprint'] == 'exact été'
    assert checkpoints.discovery_checkpoints.fetched(conn, 'release')
    for name, owner in trials.publication.OWNERS.items():
        assert owner.selected(conn) == (None if trials_native else expected['trial_revisions'][name])
        assert owner.value(conn, expected['trial_revisions'][name]) == expected['trial_originals'][owner.source]
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_account_reference_keeps_context_retention_programs(context_upgrade):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions
    assert 'CREATE TABLE sync_attempts' in definitions


@pytest.mark.parametrize('target', range(94, 103))
def test_each_registered_upgrade_keeps_native_context_and_every_retained_original(context_upgrade, target):
    conn, _, expected = context_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert_native_context(conn, expected, trials_native=target >= 95)


@pytest.fixture(scope='module')
def context_renamed(context_upgrade):
    conn, path, expected = context_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:102])
    accounts = list(map(tuple, conn.execute('SELECT id,label FROM identities ORDER BY id')))
    restore = Mock()
    regular_guards = Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', regular_guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103])
    restore.assert_called_once()
    regular_guards.assert_called_once_with(conn)
    return conn, path, expected, accounts, restore.call_args


def test_context_account_cutover_preserves_real_rows_ids_and_foreign_keys(context_renamed):
    conn, _, _, accounts, captured = context_renamed
    # Restoration is a separate real SQL owner below this rename coordinator.
    assert captured.args[0] is conn
    assert captured.args[1]
    assert list(map(tuple, conn.execute('SELECT id,label FROM accounts ORDER BY id'))) == accounts
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identities'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def context_restored(context_renamed):
    conn, path, expected, _, captured = context_renamed
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return conn, path, expected


def test_account_restoration_installs_actual_programs_and_retains_every_context_original(context_restored):
    conn, _, expected = context_restored
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    assert_native_context(conn, expected, trials_native=True)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_upgrades_preserve_restored_context_archives(context_restored, target):
    conn, _, expected = context_restored
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert_native_context(conn, expected, trials_native=True)


@pytest.fixture(scope='module')
def context_regular(context_restored):
    conn, _, _ = context_restored
    runner.migrate(conn, registry=registry.MIGRATIONS)
    complete_regular_input(conn)
    return context_restored


def test_current_context_regular_admission_is_repeatable_and_retains_originals(context_regular):
    conn, _, expected = context_regular
    regular.migrate(conn)
    regular.migrate(conn)
    regular.validate(conn)
    assert_native_context(conn, expected, trials_native=True)


def test_current_context_reopen_preserves_native_consumers_and_all_trial_decisions(context_regular):
    _, path, expected = context_regular
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        assert_native_context(store.conn, expected, trials_native=True)
        for owner in trials.publication.OWNERS.values():
            assert store.rec_proposals.get_proposals(owner.source) == expected['trial_values'][owner.source]
    finally:
        store.conn.close()
