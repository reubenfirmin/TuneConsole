"""Historical action custody retains uncertainty, order, links and backup bytes."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import action_backup_files, collection_recovery, fao, policy_documents, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import action_ledger as preparation
from yt_playlist.core.store import Store


def finish(conn, size=128):
    for _ in range(150):
        progress = preparation.JOB.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Action preparation did not finish bounded fixture')


def seed(store, *, count=3, backup_path=None):
    account = store.accounts.upsert_account('Action', 'action.json', None, True)
    member = store.playlists.upsert_playlist(account, 'SOURCE', 'Source', 0, '', 1)
    store.playlists.upsert_playlist(account, 'COPY', 'Copy', 0, '', 1)
    path = store._db_path.parent / 'retained-backup.json' if backup_path is None else Path(backup_path)
    backup = {'snapshot_version': 2, 'identity_id': account, 'ytm_playlist_id': 'SOURCE',
              'tracks': [{'video_id': 'one'}, {'video_id': 'one'}], 'paid': {'captured': '東京'}}
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=2))
    undo = {'backups': [str(path), str(path)], 'deletions': [{'identity': account, 'ytm': 'SOURCE', 'phase': 'pending', 'backup': str(path)}],
        'recovery': {'version': 1, 'steps': [
            {'kind': 'copy', 'identity': account, 'ytm': 'COPY', 'phase': 'deleting', 'deletion_backup': str(path)},
            {'kind': 'backup', 'backup': str(path), 'phase': 'creating', 'marker': 'captured-marker', 'observed': None},
            {'kind': 'keeper', 'entry': {'identity': account, 'ytm': 'SOURCE'}, 'phase': 'restoring', 'observed': []}]}}
    action = store.actions.record_action('delete_playlist', '{}', '{}', 'delete_pending', json.dumps(undo), 2)
    placement = {'playlist_id': member, 'after_video_id': None, 'video_ids': ['one'] * count,
                 'set_video_ids': {'one': 'exact-occurrence'}}
    placed = store.actions.record_action('add_tracks', '{}', json.dumps(placement), 'pending', '{}', 3)
    draft = store.actions.record_action('merge_draft', '{"paid":"東京"}', 'SOURCE|COPY', 'draft', 'non-JSON draft evidence', None)
    linked = store.actions.record_action('undo', json.dumps({'undid': action}), '{}', 'executed', '{}', 4)
    plan = {'additions': [{'identity_key': 'unresolved-recording', 'source_video_id': 'one', 'target_video_id': None, 'method': 'unresolved'}], 'unresolved': []}
    planned = store.actions.record_action('plan', json.dumps({'source': member, 'source_ytm': 'SOURCE', 'mode': 'copy'}),
                                          json.dumps(plan), 'planned', '{}', 5)
    return account, path, (action, placed, draft, linked, planned)


def rows(conn, identity, suffix, order):
    head = preparation.head(conn, identity)
    return conn.execute(f'SELECT * FROM {preparation.table(suffix)} WHERE source=? AND preparation_revision=? ORDER BY {order}',
                        (str(identity), head['preparation_revision'])).fetchall()


@pytest.mark.parametrize('installed', [79, 93, 97], indirect=True)
def test_exact_documents_unknown_outcomes_order_links_and_backups(installed):
    store, _ = installed
    account, path, identities = seed(store)
    original = {row['id']: dict(row) for row in store.conn.execute('SELECT * FROM main.actions')}
    scopes = list(map(tuple, store.conn.execute('SELECT * FROM main.playlist_scopes ORDER BY ref')))
    preparation.JOB.install(store.conn)
    finish(store.conn, size=2)
    for identity in identities:
        head = preparation.head(store.conn, identity)
        assert preparation.read_original(store.conn, head) == original[identity]
        expected = original[identity]
        assert collection_recovery.read(head['recovery_filename'], head['recovery_digest'], head['recovery_bytes'], kind='action', owner=identity) == {
            'params': expected['params_json'], 'plan': expected['plan_json'], 'undo': expected['undo_json']}
    action, placed, draft, linked, planned = identities
    steps = rows(store.conn, action, 'steps', 'position')
    assert [(row['operation'], row['phase'], row['outcome']) for row in steps] == [
        ('delete_playlist', 'uncertain', 'unknown'), ('restore_backup', 'creating', 'unknown'),
        ('delete_copy', 'deleting', 'unknown'), ('restore_keeper', 'restoring', 'confirmed_present')]
    assert [row['target_position'] for row in steps] == [0, 0, 1, 2]
    assert steps[1]['creation_marker'] == 'captured-marker' and steps[1]['observed_status'] == 'unknown'
    backups = rows(store.conn, action, 'backups', 'position')
    assert len(backups) == 1 and backups[0]['backup_status'] == 'verified'
    assert fao.read_bytes(backups[0]['retained_filename']) == fao.read_bytes(path)
    assert [row['video_id_snapshot'] for row in rows(store.conn, placed, 'placement_items', 'position')] == ['one'] * 3
    assert preparation.head(store.conn, draft)['draft_signature'] == 'SOURCE|COPY'
    assert preparation.head(store.conn, linked)['undo_of_action_id'] == action
    resolution, = rows(store.conn, planned, 'resolutions', 'list_kind,position')
    assert (resolution['recording_id'], resolution['unresolved_key'], resolution['captured_identity_key']) == (None, 'unresolved-recording', 'unresolved-recording')
    assert list(map(tuple, store.conn.execute('SELECT * FROM main.playlist_scopes ORDER BY ref'))) == scopes
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79, 97], indirect=True)
def test_frozen_upgrade_agrees_and_preparation_survives_file_collection(installed, tmp_path):
    store, _ = installed
    _, _, identities = seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    upgraded = Store(tmp_path / 'action-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        for identity in identities:
            native = upgraded.conn.execute('SELECT * FROM actions WHERE id=?', (identity,)).fetchone()
            staged = preparation.head(store.conn, identity)
            for field in ('kind','status','idempotency_key','intent_digest','undo_of_action_id','execution_mode','created_at','executed_at'):
                assert native[field] == staged[field]
            for suffix, order, fields in (
                ('targets', 'role,position', ('role','position','account_id','unresolved_account_id','playlist_scope_id','provider_playlist_id_snapshot','membership_ref_id','unresolved_membership_ref_id','resolution_reason')),
                ('steps', 'position', ('position','operation','phase','outcome','creation_marker','created_provider_id_snapshot','created_playlist_scope_id','original_survived','observed_status','backup_digest','backup_bytes','backup_status','updated_at')),
                ('placement_items', 'position', ('position','video_id_snapshot')),
                ('placement_handles', 'video_id_snapshot', ('video_id_snapshot','occurrence_handle_snapshot')),
                ('resolutions', 'list_kind,position', ('list_kind','position','recording_id','unresolved_key','captured_identity_key','source_video_id_snapshot','target_video_id_snapshot','method'))):
                native_table = 'action_plan_resolutions' if suffix == 'resolutions' else 'action_' + suffix
                actual = upgraded.conn.execute(f'SELECT {",".join(fields)} FROM {native_table} WHERE action_id=? ORDER BY {order}', (identity,)).fetchall()
                captured = rows(store.conn, identity, suffix, order)
                assert list(map(tuple, actual)) == [tuple(row[field] for field in fields) for row in captured]
        collection_recovery.collect_orphans(upgraded.conn)
        for identity in identities:
            assert preparation.read_original(upgraded.conn, preparation.head(upgraded.conn, identity))['id'] == identity
        backup = rows(upgraded.conn, identities[0], 'backups', 'position')[0]
        action_backup_files.verified(backup['retained_filename'], backup['backup_digest'], backup['backup_bytes'])
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_placement_resumes_without_originals_and_reconciles_continuing_writes(installed):
    store, _ = installed
    _, _, identities = seed(store, count=1040)
    placed = identities[1]
    preparation.JOB.install(store.conn)
    for _ in range(10):
        progress = preparation.JOB.step(store.conn)
        cursor = json.loads(progress.cursor) if progress.cursor else None
        if isinstance(cursor, dict) and json.loads(cursor['key']) == [placed]:
            break
    assert isinstance(cursor, dict)
    def authorizer(action, owner, column, *unused):
        if action == sqlite3.SQLITE_READ and owner == 'actions' and column in ('params_json','plan_json','undo_json'):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        while isinstance(json.loads(preparation.JOB.progress(store.conn).cursor), dict):
            preparation.JOB.step(store.conn)
    finally:
        store.conn.set_authorizer(None)
    finish(store.conn)
    old = preparation.head(store.conn, placed)
    assert len(rows(store.conn, placed, 'placement_items', 'position')) == 1040
    with store.conn:
        store.conn.execute("UPDATE actions SET status='cancelled' WHERE id=?", (placed,))
    finish(store.conn)
    assert preparation.head(store.conn, placed)['status'] == 'cancelled'
    assert preparation.read_original(store.conn, old)['status'] == 'pending'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_interrupt_retry_missing_manifest_and_retained_backup_rebuild(installed, monkeypatch):
    store, _ = installed
    _, path, identities = seed(store)
    job = preparation.JOB
    job.install(store.conn)
    publish = policy_documents._publish_encoded
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled action file failure')
    monkeypatch.setattr(policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='file failure'):
        job.step(store.conn)
    failed = job.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_action_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_action_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1, 'independent_action_ddl', install, validate)))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    checkpoint = job.step(store.conn, batch_size=2, retry=True)
    assert store.conn.execute('SELECT count(*) FROM prepared_action_backups').fetchone()[0] == 1
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled action interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == checkpoint
    manifest = store.conn.execute('SELECT plan_filename FROM prepared_action_generations WHERE source=?', (str(identities[0]),)).fetchone()[0]
    Path(manifest).unlink()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    reopened = Store(store._db_path)
    try:
        job.step(reopened.conn, retry=True)
        finish(reopened.conn)
        assert rows(reopened.conn, identities[0], 'backups', 'position')[0]['backup_status'] == 'verified'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_missing_references_and_invalid_backup_preserve_previous_generation(installed):
    store, _ = installed
    _, path, identities = seed(store)
    with store.conn:
        store.conn.execute('INSERT INTO actions(id,kind,params_json,plan_json,undo_json,status) VALUES (100,?,?,?,?,?)',
            ('undo', '{"undid":999}', '{}', '{}', 'planned'))
        store.conn.execute('UPDATE actions SET params_json=? WHERE id=?', ('{"source":999,"source_ytm":"MISSING"}', identities[0]))
    preparation.JOB.install(store.conn)
    finish(store.conn)
    missing = preparation.head(store.conn, 100)
    assert missing['undo_of_action_id'] is None and missing['unresolved_undo_id'] == 999
    previous = preparation.head(store.conn, identities[0])
    path.write_text('{"tracks": "bad"}')
    with store.conn:
        store.conn.execute('UPDATE actions SET executed_at=9 WHERE id=?', (identities[0],))
    with pytest.raises(ValueError, match='Unsupported playlist recovery backup'):
        preparation.JOB.step(store.conn)
    assert preparation.head(store.conn, identities[0])['preparation_revision'] == previous['preparation_revision']
    backup = rows(store.conn, identities[0], 'backups', 'position')[0]
    action_backup_files.verified(backup['retained_filename'], backup['backup_digest'], backup['backup_bytes'])


@pytest.mark.parametrize('installed', [97], indirect=True)
def test_reference_write_during_planning_withdraws_stale_plan(installed, monkeypatch):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    publish, changed = files.publish, False
    def race(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.accounts.upsert_account('Other', 'other.json', None, False)
        return publish(root, encoded)
    monkeypatch.setattr(files, 'publish', race)
    progress = preparation.JOB.step(store.conn)
    assert progress.processed == 0 and not store.conn.execute('SELECT 1 FROM prepared_action_heads').fetchone()
    finish(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_captured_remote_mismatch_never_adopts_reused_membership_and_late_undo_link_reconciles(installed):
    store, _ = installed
    account, _, identities = seed(store)
    member = store.conn.execute("SELECT id FROM main.playlists WHERE ytm_playlist_id='SOURCE'").fetchone()[0]
    with store.conn:
        store.conn.execute('UPDATE actions SET params_json=? WHERE id=?',
            (json.dumps({'source': member, 'source_ytm': 'HISTORICAL'}), identities[0]))
        store.conn.execute('INSERT INTO actions(id,kind,params_json,plan_json,undo_json,status) VALUES (100,?,?,?,?,?)',
            ('undo', '{"undid":999}', '{}', '{}', 'planned'))
    preparation.JOB.install(store.conn)
    finish(store.conn)
    captured = next(row for row in rows(store.conn, identities[0], 'targets', 'role,position') if row['role'] == 'source')
    assert (captured['account_id'], captured['playlist_scope_id'], captured['membership_ref_id'], captured['unresolved_membership_ref_id'],
            captured['provider_playlist_id_snapshot'], captured['resolution_reason']) == (None, None, None, member, 'HISTORICAL', 'membership_snapshot_mismatch')
    assert preparation.head(store.conn, 100)['unresolved_undo_id'] == 999
    with store.conn:
        store.conn.execute('INSERT INTO actions(id,kind,params_json,plan_json,undo_json,status) VALUES (999,?,?,?,?,?)',
            ('remove_track', '{}', '{}', '{}', 'executed'))
    finish(store.conn)
    assert preparation.head(store.conn, 100)['undo_of_action_id'] == 999
    assert preparation.head(store.conn, 100)['unresolved_undo_id'] is None
