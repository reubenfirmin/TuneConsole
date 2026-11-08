"""Prepare genuine predecessor operation state without sending or inventing work."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor, custody_upgrade_cases
from tests.independent_schema_support import independent_regular_ddl
from tests.store_fixture_support import assert_installed_consumer_input, publication_consumer_reads
from yt_playlist.core import collection_recovery, policy_documents, radio_storage, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import append_operations as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    pass


@pytest.mark.parametrize('installed', [79, 93, 94, 96, 97], indirect=True)
def test_append_operation_input_keeps_admitted_history_and_private_guards(installed):
    assert_installed_consumer_input(*installed)


def finish(job, conn, size=128):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Append preparation did not finish bounded fixture')


def seed(store, version, count=3, *, append=False):
    account = store.accounts.upsert_account('Append', 'append.json', None, True)
    store.playlists.upsert_playlist(account, 'TV', 'TV', 0, '', 1)
    recipe = {'tv_stream': 'stream', 'tv_refill': {'request_id': 'request', 'state': 'pending', 'at': 0,
        'before': ['same'] * count, 'additions': [{'video_id': 'next', 'paid': {'result': '東京'}}], 'items': []}}
    store.playlist_recipes.set_recipe('TV', recipe, 2)
    replacement = {'allowed': [[], ['same'] * count, []], 'confirmed': ['same', 'same'], 'waiting': [],
        'waiting_at': 0, 'receipt': {'status': 'UNKNOWN', 'videoId': 'next', 'setVideoId': 'handle', 'opaque': [1, None]},
        'adding': {'video_id': 'next'}, 'proposed': [{'video_id': 'next', 'paid': 'result'}],
        'play_prefix': None, 'slot': 0, 'choice_id': 'choice', 'excluded': ['excluded']}
    state = {'version': 2, 'episode_id': 'episode', 'identity': account, 'phase': 'appending' if append else 'replacing',
        'playlist': 'RADIO', 'tracks': [], 'position': 0, 'pending': {'video_id': 'next'} if append else None,
        'pending_sent': append, 'pending_started_at': 0 if append else None,
        'pending_result': {'status': 'UNKNOWN', 'videoId': 'next', 'setVideoId': 'handle'} if append else None,
        'replacement': None if append else replacement}
    if version < 94:
        store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(state, ensure_ascii=False, indent=2))
    else:
        with store.conn:
            radio_storage.save(store.conn, state, expected=None, now=3)
    names = ('tv_legacy' if version < 97 else 'tv_native', 'radio_legacy' if version < 94 else 'radio_native')
    sources = ('TV' if version < 97 else str(store.conn.execute('SELECT id FROM rec_recipes').fetchone()[0]),
               radio_storage.SESSION_KEY if version < 94 else 'episode')
    return names, sources, recipe, state


def items(conn, name, source):
    head = preparation.head(conn, name, source)
    return conn.execute(f'SELECT role,list_position,position,video_id_snapshot FROM {preparation.table(name,"items")} '
        'WHERE source=? AND preparation_revision=? ORDER BY role,list_position,position', (source, head['preparation_revision'])).fetchall()


@pytest.mark.parametrize('installed', [79, 93, 94, 96, 97], indirect=True)
def test_order_receipts_empty_roles_and_native_source_revisions(installed):
    store, version = installed
    names, sources, recipe, state = seed(store, version)
    original_sources = {}
    for name, source in zip(names, sources):
        job = preparation.JOBS[name]
        job.install(store.conn)
        finish(job, store.conn, size=2)
        head = preparation.head(store.conn, name, source)
        original = preparation.read_original(store.conn, name, head)
        document = json.loads(original)
        assert (document['state'] if name.endswith('_native') else document) == (recipe if name.startswith('tv_') else state)
        original_sources[name] = original
        assert head['started_at'] == 0
        assert head['operation_present'] == 1
    tv = preparation.head(store.conn, names[0], sources[0])
    assert (tv['role_mask'], tv['state'], tv['member_count']) == (7, 'pending', 4)
    assert [tuple(row) for row in items(store.conn, names[0], sources[0]) if row['role'] == 'before'] == [
        ('before', 0, index, 'same') for index in range(3)]
    assert collection_recovery.read(tv['recovery_filename'], tv['recovery_digest'], tv['recovery_bytes'],
        kind='tv-append', owner=sources[0]) == recipe['tv_refill']
    radio = preparation.head(store.conn, names[1], sources[1])
    assert (radio['episode_id'], radio['kind'], radio['sent'], radio['allowed_count'], radio['waiting_present'], radio['play_prefix_present']) == (
        'episode', 'replacement', 1, 3, 1, 0)
    assert (radio['receipt_status'], radio['receipt_video_id_snapshot'], radio['receipt_handle_snapshot'], radio['slot']) == ('UNKNOWN', 'next', 'handle', 0)
    assert collection_recovery.read(radio['recovery_filename'], radio['recovery_digest'], radio['recovery_bytes'],
        kind='radio-session', owner=sources[1]) == state
    if version >= 94:
        assert radio['source_revision'] == 1
    if version == 97:
        assert isinstance(tv['entity_id'], int) and len(tv['source_revision']) == 32
    assert store.playlist_recipes.get_recipe('TV') == recipe
    assert radio_storage.load(store.conn)[0] == state
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare_native_comparison(store, version):
    names, sources, recipe, state = seed(store, version, append=True)
    originals = {}
    for name, source in zip(names, sources):
        job = preparation.JOBS[name]
        job.install(store.conn)
        finish(job, store.conn)
        originals[name] = preparation.read_original(store.conn, name, preparation.head(store.conn, name, source))
    return names, sources, originals


@pytest.fixture(scope='module')
def native_inputs(upgraded_custody_inputs):
    from functools import partial

    def get(version, target, **options):
        return upgraded_custody_inputs(version, target, 'append-native-agreement',
            partial(prepare_native_comparison, version=version), **options)

    get.release = lambda version: upgraded_custody_inputs.release(version, 'append-native-agreement')
    return get


@pytest.mark.parametrize('version', [79, 97])
def test_native_comparison_input_keeps_exact_preparation(native_inputs, version):
    template = native_inputs(version, version)
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    names, sources, originals = template.result
    for name, source in zip(names, sources):
        assert preparation.read_original(template.source.conn, name,
            preparation.head(template.source.conn, name, source)) == originals[name]


@pytest.mark.parametrize('version,target,phase', custody_upgrade_cases((79, 97)))
def test_native_upgrade_phase_keeps_exact_preparation(native_inputs, version, target, phase):
    template = native_inputs(version, target, phase=phase)
    assert_upgrade_phase(template, target if phase == 'converted' else custody_predecessor(version, target))
    names, sources, originals = template.result
    for name, source in zip(names, sources):
        assert preparation.read_original(template.source.conn, name,
            preparation.head(template.source.conn, name, source)) == originals[name]
    assert not template.source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', [79, 97])
def test_native_serving_input_keeps_private_read_guards(native_inputs, version):
    template = native_inputs(version, 107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.mark.parametrize('version', [79, 97])
def test_frozen_upgrade_agreement_and_retained_bytes(native_inputs, version, tmp_path):
    prepared = native_inputs(version, version)
    names, sources, originals = prepared.result
    store = prepared.source
    with native_inputs(version, 107).open(tmp_path / 'append.db') as upgraded:
        native_tv = upgraded.conn.execute('SELECT * FROM tv_append_intents').fetchone()
        staged_tv = preparation.head(store.conn, names[0], sources[0])
        for field in ('stream_token', 'request_id', 'state', 'started_at', 'playlist_scope_id'):
            assert native_tv[field] == staged_tv['scope_ref' if field == 'playlist_scope_id' else field]
        native_radio = upgraded.conn.execute('SELECT * FROM radio_operations').fetchone()
        staged_radio = preparation.head(store.conn, names[1], sources[1])
        for field in ('kind', 'sent', 'started_at', 'receipt_status', 'receipt_video_id_snapshot', 'receipt_handle_snapshot',
                      'allowed_count', 'waiting_present', 'play_prefix_present', 'adding_video_id_snapshot', 'slot', 'choice_id'):
            assert native_radio[field] == staged_radio[field]
        assert native_radio['kind'] == 'append' and native_radio['sent'] == 1
        collection_recovery.collect_orphans(upgraded.conn)
        for name, source in zip(names, sources):
            assert preparation.read_original(upgraded.conn, name, preparation.head(upgraded.conn, name, source)) == originals[name]


@pytest.mark.parametrize('installed', [93, 97], indirect=True)
def test_large_members_resume_without_source_io_and_continuing_replacement(installed, monkeypatch):
    store, version = installed
    names, sources, recipe, state = seed(store, version, count=1040)
    for name, source in zip(names, sources):
        job = preparation.JOBS[name]
        job.install(store.conn)
        job.step(store.conn, batch_size=128)
        def denied(*args, **kwargs):
            pytest.fail('Resumed append batch read its source recovery file')
        read = collection_recovery.read
        monkeypatch.setattr(collection_recovery, 'read', denied)
        def authorizer(action, owner, column, *unused):
            if action == sqlite3.SQLITE_READ and (owner, column) in {('settings', 'value'), ('rec_recipes', 'recipe')}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        store.conn.set_authorizer(authorizer)
        try:
            while isinstance(json.loads(job.progress(store.conn).cursor), dict):
                job.step(store.conn, batch_size=128)
        finally:
            store.conn.set_authorizer(None)
            monkeypatch.setattr(collection_recovery, 'read', read)
        finish(job, store.conn)
        old = preparation.head(store.conn, name, source)
        original = preparation.read_original(store.conn, name, old)
        if name.startswith('tv_'):
            store.playlist_recipes.set_recipe('TV', {**recipe, 'tv_refill': {'request_id': 'request', 'state': 'complete', 'items': []}}, 4)
        else:
            newer = {**state, 'pending': None, 'replacement': None}
            with store.conn:
                if version < 94:
                    store.conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(newer), source))
                else:
                    radio_storage.save(store.conn, newer, expected=('episode', 1), now=4)
        assert job.progress(store.conn).prepared_revision is None
        finish(job, store.conn)
        latest = preparation.head(store.conn, name, source)
        assert latest['state'] == 'complete' if name.startswith('tv_') else latest['operation_present'] == 0
        assert preparation.read_original(store.conn, name, old) == original


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_interrupt_missing_plan_retry_and_new_regular_ddl(
        installed, monkeypatch, independent_regular_ddl):
    store, version = installed
    names, sources, _, _ = seed(store, version)
    name, source = names[0], sources[0]
    job = preparation.JOBS[name]
    job.install(store.conn)
    publish = policy_documents._publish_encoded
    def fail(*args, age=None):
        assert not store.conn.in_transaction
        raise OSError('controlled append file failure')
    monkeypatch.setattr(policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='file failure'):
        job.step(store.conn)
    failed = job.progress(store.conn)
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    with pytest.raises(MigrationError, match='retry explicitly'):
        job.step(store.conn)
    checkpoint = job.step(store.conn, batch_size=1, retry=True)
    def interrupt(*args):
        preparation.copy(name, *args)
        raise KeyboardInterrupt('controlled append interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == checkpoint
    row = store.conn.execute(f'SELECT plan_filename FROM {preparation.table(name,"generations")}').fetchone()
    Path(row[0]).unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    reopened = Store(store._db_path)
    try:
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
        assert preparation.head(reopened.conn, name, source)['state'] == 'pending'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_invalid_or_absent_operation_does_not_invent_state_or_overwrite_complete_generation(installed):
    store, version = installed
    names, sources, _, state = seed(store, version)
    name, source = names[1], sources[1]
    job = preparation.JOBS[name]
    job.install(store.conn)
    finish(job, store.conn)
    previous = preparation.head(store.conn, name, source)
    invalid = [1, {**state, 'pending': {'video_id': 'other'}}, {**state, 'replacement': {'allowed': [None]}},
               {**state, 'replacement': {**state['replacement'], 'waiting_at': float('inf')}}]
    for value in invalid:
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), source))
        with pytest.raises(ValueError):
            job.step(store.conn, retry=True)
        assert preparation.head(store.conn, name, source)['preparation_revision'] == previous['preparation_revision']
    for raw in (None, '', 'null', '{}'):
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, source))
        job.step(store.conn, retry=True)
        finish(job, store.conn)
        latest = preparation.head(store.conn, name, source)
        assert latest['operation_present'] == 0 and latest['started_at'] is None
        assert preparation.read_original(store.conn, name, latest) == raw


@pytest.mark.parametrize('installed', [93, 97], indirect=True)
def test_reference_write_during_file_planning_rejects_stale_capture(installed, monkeypatch):
    store, version = installed
    names, sources, _, _ = seed(store, version)
    publish = files.publish
    for name, source in zip(names, sources):
        job = preparation.JOBS[name]
        job.install(store.conn)
        changed = False
        def race(root, encoded):
            nonlocal changed
            assert not store.conn.in_transaction
            if not changed:
                changed = True
                store.accounts.upsert_account('Reference ' + name, name + '.json', None, False)
            return publish(root, encoded)
        monkeypatch.setattr(files, 'publish', race)
        progress = job.step(store.conn)
        assert changed and progress.processed == 0
        assert preparation.head(store.conn, name, source) is None
        finish(job, store.conn)
        assert preparation.head(store.conn, name, source)['operation_present'] == 1


@pytest.mark.parametrize('version', [79, 97])
def test_native_sources_finish_with_committed_read_only_inputs(native_inputs, version):
    for source in native_inputs.release(version):
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
