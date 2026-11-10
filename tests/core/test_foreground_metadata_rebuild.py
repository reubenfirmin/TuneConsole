"""Foreground native deliveries survive real split analytics and restart."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.db_rebuild_support import populate_catalog
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import prepared_template
from tests.rebuild_startup_support import startup_destination
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import db_handles, metadata_native, metadata_runtime, metadata_runtime_files
from yt_playlist.core.db_rebuild import build, destination, job
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.migrations import runner
from yt_playlist.core.preparation.metadata_runtime_verification import verify_native
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store
from yt_playlist.rec.choice_evidence import STORAGE_PENDING


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners do not start the web application."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('foreground-metadata-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


def source_key(entity):
    return json.dumps(['native', 'recording', entity], separators=(',', ':'))


@pytest.fixture(scope='module')
def metadata_source():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        install_numbered_input(conn, 93)
        with conn:
            populate_catalog(conn)
            conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STORAGE_PENDING, '1'))
        yield conn


def test_native_source_keeps_the_real_populated_historical_catalog(metadata_source):
    runner.validate_version(metadata_source, 93)
    assert metadata_source.execute('SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8
    assert not metadata_runtime.active(metadata_source)


@pytest.fixture(scope='module')
def metadata_admitted(metadata_source, tmp_path_factory):
    path = tmp_path_factory.mktemp('foreground-source') / 'source.db'

    def seed(store):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            for entity, energy in [('before-native-id', .4), ('during-native-id', .6)]:
                metadata_native.record(store.conn, 108, 'native', [MetadataObservation(
                    field, value, 'recording', entity, 'prediction', unit='classifier_probability')
                    for field, value in [('mood_party', .2), ('energy', energy)]],
                    run_id='original', now=17, recording_id=8)

    with prepared_template(metadata_source, seed, database=path, defer_metadata=True) as template:
        yield template


def test_native_source_admission_keeps_wal_and_real_revision_guards(metadata_admitted):
    assert metadata_admitted.source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert metadata_admitted.revision_guards
    assert metadata_admitted.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    for entity, energy in [('before-native-id', .4), ('during-native-id', .6)]:
        values = metadata_native.source_values(metadata_admitted.source.conn, source_key(entity))
        assert values['energy']['value'] == energy
        assert values['mood_party']['value'] == .2


@pytest.fixture(scope='module')
def metadata_plan(metadata_admitted):
    return destination.for_source(metadata_admitted.source._db_path, split=True)


def test_real_destination_copies_native_values_only_before_runtime_cutover(metadata_plan):
    names = [stage.name for stage in metadata_plan.stages]
    assert names.count('metadata:native') == 1
    assert names.index('metadata:native') < metadata_plan.runtime_stages
    assert names.index('served-facts') >= metadata_plan.runtime_stages
    assert all(stage.name != 'metadata:native'
               for stage in metadata_plan.stages[metadata_plan.runtime_stages:])


@pytest.fixture(scope='module')
def metadata_contract(metadata_plan, startup_destination):
    # Supply completed unrelated catalog conversion below this native owner.
    # Keep the production native converter, activation, served-facts stage,
    # construct/checkpoint protocol, closed-file installer and analytics real.
    stages = {stage.name: stage for stage in metadata_plan.stages}
    completed_runtime = startup_destination.stages[0]

    def copy_catalog_and_sources(reader, target, cursor, limit):
        result = completed_runtime.copy(reader, target, cursor, limit)
        columns = [row[1] for row in reader.execute('PRAGMA table_info(rec_musical_sources)')]
        target.executemany('INSERT INTO rec_musical_sources VALUES (' + ','.join('?' for _ in columns) + ')',
                           reader.execute('SELECT * FROM rec_musical_sources'))
        return result

    def verify_runtime(reader, target):
        startup_destination.verify_runtime(reader, target)
        verify_native(target, reader)
        assert metadata_runtime.active(target)

    return replace(startup_destination, version='foreground-native-owner-v1',
        stages=(build.Stage('completed-catalog-and-sources', copy_catalog_and_sources),
                stages['metadata:native'], stages['metadata:activate'],
                stages['served-facts'], startup_destination.stages[1]),
        runtime_stages=3, verify_runtime=verify_runtime)


def test_controlled_catalog_retains_the_real_production_native_and_analytics_owners(metadata_contract, metadata_plan):
    production = {stage.name: stage for stage in metadata_plan.stages}
    assert metadata_contract.stages[1] is production['metadata:native']
    assert metadata_contract.stages[2] is production['metadata:activate']
    assert metadata_contract.stages[3] is production['served-facts']


@pytest.fixture(scope='module', params=[False, True], ids=['uninterrupted', 'reopened'])
def foreground_branch(request, metadata_admitted, metadata_contract, tmp_path_factory):
    path = tmp_path_factory.mktemp('foreground-branch') / 'branch.db'
    with metadata_admitted.open(path) as store, pytest.MonkeyPatch.context() as patch:
        patch.setattr(destination, 'for_source', lambda path, **kwargs: metadata_contract)
        branch = dict(store=store, path=path, restart=request.param, originals={}, during=False)
        yield branch
        # Restart replaces the original serving Store after closing it.
        if branch['store'] is not store:
            branch['store'].conn.close()


def test_foreground_branch_has_private_unchanged_installed_input(foreground_branch):
    store = foreground_branch['store']
    assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert not store.activity_paused
    assert not metadata_runtime.active(store.conn)
    assert metadata_native.source_values(store.conn, source_key('before-native-id'))['mood_party']['value'] == .2


@pytest.fixture(scope='module')
def runtime_cutover(foreground_branch):
    branch = foreground_branch
    store = branch['store']
    branch['first'] = job.run(store, rebuild=True)
    branch['originals'] = {(key, revision): metadata_runtime_files.read(store.conn, 'native', (key,), revision)
        for key, revision in store.conn.execute('SELECT source_key,revision FROM rec_musical_sources')}
    return branch


def test_native_runtime_cutover_keeps_original_documents_and_resumes_writes(runtime_cutover):
    branch, store = runtime_cutover, runtime_cutover['store']
    assert branch['first']['analytics_pending']
    assert not store.activity_paused
    assert metadata_runtime.active(store.conn)
    assert len(branch['originals']) == 2
    for entity in ('before-native-id', 'during-native-id'):
        assert metadata_native.source_values(store.conn, source_key(entity))['mood_party']['value'] == .2


def lookup(branch, entity, score):
    store = branch['store']
    assert not store.activity_paused
    store.enrichment_state.record_metadata_observations(108, 'native', [MetadataObservation(
        'mood_party', score, 'recording', entity, 'prediction', unit='classifier_probability')],
        run_id='foreground-' + entity, now=30)
    assert metadata_native.source_values(store.conn, source_key(entity))['mood_party']['value'] == score


@pytest.fixture(scope='module')
def foreground_accepted(runtime_cutover):
    lookup(runtime_cutover, 'before-native-id', .91)
    lookup(runtime_cutover, 'new-native-id', .73)
    return runtime_cutover


def test_existing_and_new_foreground_results_are_accepted_before_analytics(foreground_accepted):
    branch = foreground_accepted
    for entity, score in [('before-native-id', .91), ('new-native-id', .73)]:
        assert metadata_native.source_values(branch['store'].conn, source_key(entity))['mood_party']['value'] == score


@pytest.fixture(scope='module')
def foreground_analytics(foreground_accepted):
    branch = foreground_accepted

    def progress(phase, detail):
        if detail['stage'] == 'served-facts':
            if branch['restart']:
                raise InterruptedError('after accepted foreground result')
            if not branch['during']:
                lookup(branch, 'during-native-id', .82)
                branch['during'] = True

    if branch['restart']:
        with pytest.raises(InterruptedError, match='after accepted foreground result'):
            job.finish_analytics(branch['store'], progress=progress)
    else:
        job.finish_analytics(branch['store'], progress=progress)
    return branch


def test_analytics_completes_or_persists_an_actual_interrupted_checkpoint(foreground_analytics):
    branch = foreground_analytics
    assert db_handles.state(branch['path'])['analytics_pending'] == branch['restart']
    assert branch['during'] == (not branch['restart'])
    if branch['restart']:
        pending = build._checkpoint(branch['store'].conn)
        assert pending['stage'] > 3
        assert pending['stages'][pending['stage'] - 1] == 'served-facts'


@pytest.fixture(scope='module')
def foreground_reopened(foreground_analytics):
    branch = foreground_analytics
    if branch['restart']:
        branch['store'].conn.close()
        branch['store'] = Store(branch['path'])
        branch['store'].init_schema(defer_metadata=True)
    return branch


def test_reopen_keeps_accepted_provider_heads_and_pending_work(foreground_reopened):
    branch = foreground_reopened
    assert not branch['store'].activity_paused
    for entity, score in [('before-native-id', .91), ('new-native-id', .73)]:
        assert metadata_native.source_values(branch['store'].conn, source_key(entity))['mood_party']['value'] == score


@pytest.fixture(scope='module')
def foreground_finished(foreground_reopened):
    branch = foreground_reopened
    if branch['restart']:
        maintenance = StorageMaintenance(branch['store'])
        try:
            maintenance.start()
            maintenance.join(30)
            assert maintenance.phase == 'done', maintenance.error
            assert maintenance.ready.is_set()
            assert maintenance.report['id'] == branch['first']['id']
        finally:
            maintenance.stop()
            maintenance.join(30)
    return branch


def test_analytics_and_restart_preserve_exact_values_heads_and_immutable_originals(foreground_finished):
    branch, store = foreground_finished, foreground_finished['store']
    assert not db_handles.state(branch['path'])['analytics_pending']
    for entity, score in [('before-native-id', .91), ('new-native-id', .73),
                          ('during-native-id', .2 if branch['restart'] else .82)]:
        key = source_key(entity)
        assert metadata_native.source_values(store.conn, key)['mood_party']['value'] == score
        expected_run = 'original' if entity == 'during-native-id' and branch['restart'] else 'foreground-' + entity
        assert store.conn.execute('SELECT run_id FROM metadata_native_delivery_heads WHERE source_key=?',
                                  (key,)).fetchone()[0] == expected_run
    for (key, revision), document in branch['originals'].items():
        assert metadata_runtime_files.read(store.conn, 'native', (key,), revision) == document
    for entity, energy in [('before-native-id', .4), ('during-native-id', .6)]:
        assert metadata_native.source_values(store.conn, source_key(entity))['energy']['value'] == energy
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
