"""Workflow custody preserves recovery evidence without invoking providers."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import collection_recovery, policy_documents, recommendation_outputs as files, workflow_settings
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import workflow_receipts as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    pass


def finish(job, conn, size=128):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Workflow preparation did not finish bounded fixture')


def seed(store, count=3, *, master=True):
    account = store.accounts.upsert_account('Workflow', 'workflow.json', None, master)
    store.playlists.upsert_playlist(account, 'PLAYLIST', 'Playlist', 0, '', 1)
    gc_key = workflow_settings.GC + json.dumps([account, 'PLAYLIST'], separators=(',', ':'))
    playlist_key = workflow_settings.PLAYLIST + ':' + str(account)
    receipt = {'creation_phase': 'created', 'marker': 'captured marker', 'ytm_playlist_id': 'PLAYLIST',
               'source_video_ids': [None if index % 3 == 0 else 'repeated-video' for index in range(count)],
               'complete': True, 'order_verified': None, 'updated_at': 0, 'last_run_added': 0,
               'opaque': {'paid_result': [1, '東京', None]}, workflow_settings.TOKEN: 'captured-old-token'}
    values = {gc_key: {'identity': account, 'ytm': 'PLAYLIST', 'attempts': 2, 'last_attempt_at': 3, 'next_retry_at': 4,
                      'phase': 'reconcile', 'failure_class': 'unknown', 'error': 'Retain original', 'extra': {'witness': 1}},
              playlist_key: {'fingerprint:東京': receipt, 'unknown': None},
              workflow_settings.ALBUM: ['first', 'first', 'last'],
              workflow_settings.PLAYLIST + ':999': {'historical': {'ytm_playlist_id': 'MISSING'}}}
    originals = {key: json.dumps(value, ensure_ascii=False, indent=2) for key, value in values.items()}
    with store.conn:
        store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', originals.items())
    return values, originals, account, playlist_key


def rows(conn, name, source, suffix):
    head = preparation.head(conn, name, source)
    return conn.execute('SELECT * FROM ' + preparation.table(name, suffix) + ' WHERE source=? AND preparation_revision=? ORDER BY 1,2,3',
                        (source, head['preparation_revision'])).fetchall()


@pytest.mark.parametrize('installed', [79, 93, 97], indirect=True)
def test_exact_sources_gc_targets_receipt_order_and_null_slots(installed):
    store, _ = installed
    values, originals, account, playlist_key = seed(store)
    scope_count = store.conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0]
    for name, job in preparation.JOBS.items():
        job.install(store.conn)
        finish(job, store.conn, size=2)
        for source, raw in originals.items():
            if not (source.startswith(preparation.PREFIXES[name])):
                continue
            head = preparation.head(store.conn, name, source)
            assert preparation.read_original(store.conn, name, head) == raw
            assert store.settings.get_setting(source) == raw
    gc_key = next(source for source in originals if source.startswith(workflow_settings.GC))
    attempt = rows(store.conn, 'gc', gc_key, 'attempts')[0]
    assert (attempt['captured_account_id'], attempt['account_id'], attempt['playlist_scope_id'], attempt['resolution_reason']) == (
        account, account, 'PLAYLIST', None)
    assert collection_recovery.read(attempt['recovery_filename'], attempt['recovery_digest'], attempt['recovery_bytes'],
        kind='playlist-gc', owner=gc_key) == values[gc_key]
    receipt = rows(store.conn, 'playlists', playlist_key, 'receipts')[0]
    recovered = dict(values[playlist_key]['fingerprint:東京'])
    recovered.pop(workflow_settings.TOKEN)
    owner = json.dumps([playlist_key, 'fingerprint:東京'], ensure_ascii=False, separators=(',', ':'))
    assert collection_recovery.read(receipt['recovery_filename'], receipt['recovery_digest'], receipt['recovery_bytes'],
        kind='spotify-transfer', owner=owner) == recovered
    assert [row['video_id_snapshot'] for row in rows(store.conn, 'playlists', playlist_key, 'items')] == [None, 'repeated-video', 'repeated-video']
    albums = rows(store.conn, 'albums', workflow_settings.ALBUM, 'receipts')
    assert [(row['fingerprint'], row['duplicate_position']) for row in albums] == [('first', None), ('first', 0), ('last', None)]
    unknown = rows(store.conn, 'playlists', workflow_settings.PLAYLIST + ':999', 'receipts')[0]
    assert (unknown['captured_account_id'], unknown['account_id'], unknown['playlist_scope_id'], unknown['resolution_reason']) == (
        999, None, None, 'missing_account')
    assert store.conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0] == scope_count
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79, 97], indirect=True)
def test_frozen_upgrade_agrees_and_keeps_exact_preparation_evidence(installed, tmp_path):
    store, _ = installed
    values, originals, account, playlist_key = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'workflow-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        assert next(iter(workflow_settings.gc_values(upgraded.conn).values())) == next(value for key, value in values.items() if key.startswith(workflow_settings.GC))
        native = upgraded.conn.execute("SELECT * FROM spotify_transfer_receipts WHERE source_kind='playlist' AND captured_account_id=? AND fingerprint=?",
            (account, 'fingerprint:東京')).fetchone()
        staged = rows(store.conn, 'playlists', playlist_key, 'receipts')[0]
        fields = ('source_kind', 'captured_account_id', 'account_id', 'fingerprint', 'creation_phase', 'marker',
                  'playlist_scope_id', 'provider_playlist_id_snapshot', 'complete', 'order_verified', 'updated_at', 'last_run_added')
        assert tuple(native[field] for field in fields) == tuple(staged[field] for field in fields)
        assert [row[0] for row in upgraded.conn.execute('SELECT video_id_snapshot FROM spotify_transfer_items WHERE receipt_id=? ORDER BY position',
            (native['id'],))] == [row['video_id_snapshot'] for row in rows(store.conn, 'playlists', playlist_key, 'items')]
        collection_recovery.collect_orphans(upgraded.conn)
        for name in preparation.JOBS:
            for source, raw in originals.items():
                if source.startswith(preparation.PREFIXES[name]):
                    assert preparation.read_original(upgraded.conn, name, preparation.head(upgraded.conn, name, source)) == raw
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_ordered_slots_resume_without_payload_and_withdraw_on_continuing_writes(installed):
    store, _ = installed
    _, originals, _, source = seed(store, count=1040)
    job = preparation.JOBS['playlists']
    job.install(store.conn)
    # Reconciliation scans :999 first; complete that source before measuring
    # the captured large member plan, without assuming source ordering.
    for _ in range(10):
        progress = job.step(store.conn, batch_size=128)
        cursor = json.loads(progress.cursor) if progress.cursor else None
        if isinstance(cursor, dict) and source in cursor['key']:
            break
    assert isinstance(cursor, dict)
    def authorizer(action, owner, column, *unused):
        if action == sqlite3.SQLITE_READ and owner == 'settings' and column == 'value':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        while isinstance(json.loads(job.progress(store.conn).cursor), dict):
            job.step(store.conn)
    finally:
        store.conn.set_authorizer(None)
    finish(job, store.conn)
    old = preparation.head(store.conn, 'playlists', source)
    assert len(rows(store.conn, 'playlists', source, 'items')) == 1040
    with store.conn:
        store.conn.execute('UPDATE settings SET value=? WHERE key=?', ('{}', source))
    finish(job, store.conn)
    assert not rows(store.conn, 'playlists', source, 'receipts')
    assert preparation.read_original(store.conn, 'playlists', old) == originals[source]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_retry_and_independent_regular_ddl_preserve_recovery_sources(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['playlists']
    job.install(store.conn)
    publish = policy_documents._publish_encoded
    def fail(*args, age=None):
        assert not store.conn.in_transaction
        raise OSError('controlled workflow file failure')
    monkeypatch.setattr(policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='file failure'):
        job.step(store.conn)
    failed = job.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_workflow_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_workflow_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1, 'independent_workflow_ddl', install, validate)))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_null_blank_empty_receipts_and_invalid_owner_evidence_do_not_replace_last_complete(installed):
    store, _ = installed
    _, _, account, source = seed(store)
    job = preparation.JOBS['playlists']
    job.install(store.conn)
    finish(job, store.conn)
    for raw in (None, '', 'null', '{}', '[]'):
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, source))
        finish(job, store.conn)
        assert preparation.read_original(store.conn, 'playlists', preparation.head(store.conn, 'playlists', source)) == raw
    previous = preparation.head(store.conn, 'playlists', source)
    for value in ({'fp': {'creation_phase': 'creating'}}, {'fp': {'source_video_ids': ['']}}, {'fp': {'updated_at': float('inf')}}, 1):
        raw = json.dumps(value)
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, source))
        with pytest.raises(ValueError):
            job.step(store.conn, retry=True)
        assert preparation.head(store.conn, 'playlists', source)['preparation_revision'] == previous['preparation_revision']
        assert store.conn.execute('SELECT value FROM settings WHERE key=?', (source,)).fetchone()[0] == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_reference_write_during_planning_interrupt_and_missing_file_rebuild(installed, monkeypatch):
    store, _ = installed
    _, originals, _, source = seed(store)
    job = preparation.JOBS['playlists']
    job.install(store.conn)
    publish, changed = files.publish, False
    def race(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.accounts.upsert_account('Other', 'other.json', None, False)
        return publish(root, encoded)
    monkeypatch.setattr(files, 'publish', race)
    assert job.step(store.conn).processed == 0
    checkpoint = job.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy('playlists', *args)
        raise KeyboardInterrupt('controlled workflow interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == checkpoint
    row = store.conn.execute('SELECT plan_filename FROM prepared_workflow_playlists_generations WHERE source=?', (source,)).fetchone()
    Path(row[0]).unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    job.step(store.conn, retry=True)
    finish(job, store.conn)
    assert preparation.read_original(store.conn, 'playlists', preparation.head(store.conn, 'playlists', source)) == originals[source]
