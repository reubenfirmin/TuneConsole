"""Retained action and discovery selections share one actual populated upgrade."""
from contextlib import closing
import sqlite3

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core import test_action_publication as actions
from tests.core import test_discovery_publication as discovery
from tests.core.test_early_schema_upgrade import rename_owner, restore_programs
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import assert_upgrade_phase
from tests.store_fixture_support import install_regular_input, reuse_reporting_reader
from yt_playlist.core import action_work, schema_inspection
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """This SQL/file journey does not start an HTTP application."""


@pytest.fixture(scope='module', autouse=True)
def isolated_native_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('action-discovery-native-home')))
        bound_catalog(patch)
        yield


def test_native_publication_schema_input_keeps_actual_93_history_and_guards(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(93), 93)


@pytest.fixture(scope='module')
def native_journey(_bounded_installed_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('action-discovery-native') / 'installed.db'
    with _bounded_installed_schema(93).open(path) as source:
        source.conn.execute('PRAGMA synchronous=NORMAL')
        with source.conn:
            # Ordinary completed guards are a lower input here; the actual
            # numbered takeovers and retained publication guards remain real.
            install_regular_input(source.conn, regular.CHANGES)
        # Numbered SQL owns the upgrade. Keep application diagnostics outside
        # this boundary while retaining the same private WAL file and FKs.
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            # Column renames repeatedly inspect the complete retained schema.
            # Keep its private pages resident instead of evicting them between
            # real ALTER statements; WAL commits and file reopening stay real.
            conn.execute('PRAGMA cache_size=-16384')
            yield dict(conn=conn, path=path, source=source, version=93)


def assert_history(state):
    conn = state['conn']
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS[:state['version']]]
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_native_publication_regular_input_keeps_both_complete_histories(native_journey):
    assert_history(native_journey)
    assert regular.validate(native_journey['conn']) == len(regular.CHANGES)


def bare_numbered_connection(store):
    Revisions.unbind(store.conn)
    store._snapshot_revisions = None
    for kind, name in store.conn.execute("SELECT type,name FROM sqlite_temp_schema "
            "WHERE type IN ('trigger','view') ORDER BY type").fetchall():
        quoted = '"' + name.replace('"', '""') + '"'
        store.conn.execute(f'DROP {kind.upper()} IF EXISTS temp.{quoted}')


def test_actual_action_selection_keeps_five_originals_and_the_external_backup(native_journey):
    store = native_journey['source']
    _, backup, identities = actions.seed(store)
    selected = actions.stage(store.conn)
    actions.publication.OWNER.publish(store.conn, stage=selected)
    native_journey['actions'] = dict(stage=selected, backup=backup, bytes=backup.read_bytes(),
        originals={identity: actions.publication.OWNER.value(store.conn, selected, str(identity))
                   for identity in identities})
    assert len(identities) == 5 and actions.publication.OWNER.selected(store.conn) == selected
    bare_numbered_connection(store)
    assert_history(native_journey)


def assert_action_custody(state):
    saved = state['actions']
    assert saved['backup'].read_bytes() == saved['bytes']
    for identity, original in saved['originals'].items():
        assert actions.publication.OWNER.value(state['conn'], saved['stage'], str(identity)) == original


@pytest.mark.parametrize('version', range(94, 101))
def test_actual_pre_discovery_owners_keep_action_custody_and_native_precedence(native_journey, version):
    runner.migrate(native_journey['conn'], registry=registry.MIGRATIONS[:version])
    native_journey['version'] = version
    assert_history(native_journey)
    assert_action_custody(native_journey)
    if version >= 98:
        assert action_work.available(native_journey['conn'])
        assert actions.publication.OWNER.selected(native_journey['conn']) is None


def test_actual_discovery_100_input_keeps_all_raw_visits_and_original_owners(native_journey):
    with catalog_reader(native_journey['path'], 100, snapshot_guards=True) as store:
        with store.conn:
            store.conn.execute('UPDATE identities SET is_master=0')
        discovery.seed(store)
        assert store.conn.execute('SELECT count(*) FROM main.discovery_visible_impressions').fetchone()[0] == 5
        assert store.discovery.get_discovered_artists()[0]['offered_count'] == 9
    assert_history(native_journey)


def test_actual_discovery_artist_preparation_commits_retained_files(native_journey):
    artifact, stage = discovery.artist_ready(native_journey['conn'])
    artifact.publish(native_journey['conn'], stage=stage)
    assert artifact.selected(native_journey['conn']) == stage
    assert_history(native_journey)


def test_actual_discovery_subject_preparation_keeps_all_completed_jobs(native_journey):
    owner = discovery.prepare_subjects(native_journey['conn'])
    assert all(job.progress(native_journey['conn']).status == 'prepared' for job in owner.jobs)
    native_journey['discovery_owner'] = owner
    assert_history(native_journey)


def test_actual_discovery_selection_keeps_exact_native_expectation_and_every_input_map(native_journey):
    with catalog_reader(native_journey['path'], 100, snapshot_guards=True) as store:
        with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
            owner, stage = discovery.stage_discovery(store.conn)
            owner.publish(store.conn, stage=stage)
            native_journey['discovery'] = dict(owner=owner, stage=stage,
                expected=store.discovery.get_discovered_artists(),
                retained={kind: list(map(tuple, store.conn.execute('SELECT * FROM '+owner.table('input_'+kind))))
                          for kind in owner.kinds})
            assert owner.selected(store.conn) == stage
    assert_history(native_journey)


def assert_discovery_custody(state):
    saved = state['discovery']
    for kind, expected in saved['retained'].items():
        assert list(map(tuple, state['conn'].execute(
            'SELECT * FROM '+saved['owner'].table('input_'+kind)))) == expected


@pytest.mark.parametrize('version', [101, 102])
def test_actual_discovery_takeover_keeps_both_retained_publications(native_journey, version):
    runner.migrate(native_journey['conn'], registry=registry.MIGRATIONS[:version])
    native_journey['version'] = version
    assert_history(native_journey)
    assert_action_custody(native_journey)
    assert_discovery_custody(native_journey)
    assert native_journey['discovery']['owner'].selected(native_journey['conn']) is None


def test_actual_native_account_rename_keeps_every_original_map(native_journey):
    with schema_inspection.reuse_tables(native_journey['conn']):
        rename_owner(native_journey)
    assert_history(native_journey)
    assert_action_custody(native_journey)
    assert_discovery_custody(native_journey)


def test_actual_native_program_restoration_keeps_retained_publication_guards(native_journey):
    restore_programs(native_journey)
    runner.validate_version(native_journey['conn'], 103, data=False, foreign_keys=False)
    assert_action_custody(native_journey)
    assert_discovery_custody(native_journey)


@pytest.mark.parametrize('version', range(104, len(registry.MIGRATIONS)+1))
def test_actual_later_native_owners_preserve_both_publications(native_journey, version):
    runner.migrate(native_journey['conn'], registry=registry.MIGRATIONS[:version])
    native_journey['version'] = version
    assert_history(native_journey)
    assert_action_custody(native_journey)
    assert_discovery_custody(native_journey)


def test_fresh_native_store_prefers_native_actions_and_discovery_without_losing_retained_inputs(native_journey):
    store = Store(native_journey['path'])
    with closing(store.conn), schema_inspection.reuse_tables(store.conn):
        store.init_schema(defer_metadata=True)
        assert action_work.available(store.conn)
        assert actions.publication.OWNER.selected(store.conn) is None
        for identity in native_journey['actions']['originals']:
            assert store.actions.get_action(identity).id == identity
        saved = native_journey['discovery']
        assert saved['owner'].selected(store.conn) is None
        assert store.discovery.get_discovered_artists() == saved['expected']
        regular.migrate(store.conn)
        regular.validate(store.conn)
        assert_action_custody(native_journey)
        assert_discovery_custody(native_journey)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_regular_admission_rejects_a_lost_retained_action_map_guard(native_journey):
    with native_journey['conn']:
        native_journey['conn'].execute('DROP TRIGGER prepared_action_parts_mapped_delete')
    with pytest.raises(MigrationError, match='publication object'):
        regular.validate(native_journey['conn'])
