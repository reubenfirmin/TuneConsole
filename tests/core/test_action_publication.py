"""Select historical action custody without losing IDs or admitting remote writes."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_action_ledger_preparation import finish, rows, seed
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import action_backup_files, action_work
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import action_ledger as preparation, action_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.library import playlist_deletion


def stage(conn, *, size=128):
    preparation.JOB.install(conn)
    finish(conn)
    for _ in range(100):
        result = publication.OWNER.step(conn, batch_size=size)
        if result.complete:
            return result.id
    pytest.fail('Action map did not finish bounded fixture')


@pytest.mark.parametrize('installed', [79, 93, 97], indirect=True)
def test_preopened_readers_use_selected_actions_and_preserve_external_formats(installed):
    store, _ = installed
    account, path, identities = seed(store)
    action, placed, draft, linked, planned = identities
    originals = {row['id']:dict(row) for row in store.conn.execute('SELECT * FROM main.actions')}
    sequence = store.conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name='actions'").fetchone()
    sequence = tuple(sequence) if sequence is not None else None
    preopened = Store(store._db_path)
    try:
        preopened.init_schema(defer_metadata=True)
        selected = stage(store.conn, size=2)
        publication.OWNER.publish(store.conn, stage=selected)
        def authorizer(operation, owner, column, *unused):
            if operation == sqlite3.SQLITE_READ and owner == 'actions' and column in ('params_json','plan_json','undo_json'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        preopened.conn.set_authorizer(authorizer)
        try:
            actual = preopened.actions.get_actions()
            assert [a.id for a in actual] == list(reversed(identities))
            assert preopened.actions.get_draft('SOURCE|COPY').id == draft
            assert preopened.actions.get_draft('unknown') is None
            assert preopened.actions.get_pending_deletion(account, 'SOURCE').id == action
            assert preopened.actions.get_failed_deletion(account, 'SOURCE') is None
            assert [a.id for a in preopened.actions.get_pending_actions('add_tracks')] == [placed]
            member = json.loads(originals[placed]['plan_json'])['playlist_id']
            assert [a.id for a in preopened.actions.get_pending_placements(member)] == [placed]
            assert preopened.actions.get_action(999) is None
        finally:
            preopened.conn.set_authorizer(None)
        for a in actual:
            assert (a.kind,a.status,a.created_at,a.executed_at,a.params_json,a.plan_json) == tuple(
                originals[a.id][key] for key in ('kind','status','created_at','executed_at','params_json','plan_json'))
            assert publication.OWNER.value(store.conn, selected, str(a.id)) == originals[a.id]
        recovered = json.loads(preopened.actions.get_action(action).undo_json)
        assert recovered['deletions'][0]['phase'] == 'uncertain'
        assert recovered['recovery']['version'] == 2
        assert recovered['recovery']['steps'][0]['kind'] == 'backup'
        assert action_backup_files.verified(recovered['backups'][0])[0] == path.read_bytes()
        assert path.read_bytes() == action_backup_files.verified(recovered['deletions'][0]['backup'])[0]
        after = store.conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name='actions'").fetchone()
        assert (tuple(after) if after is not None else None) == sequence
        regular.validate(preopened.conn)
    finally:
        preopened.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.actions.get_action(draft).undo_json == 'non-JSON draft evidence'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['fields','target','step','placement','resolution','extra','omitted','original','recovery','plan'])
def test_wrong_fields_members_coverage_or_files_cannot_publish(installed, failure):
    store, _ = installed
    _, _, identities = seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    header = preparation.head(store.conn, identities[0])
    with store.conn:
        if failure == 'fields':
            store.conn.execute('UPDATE prepared_action_generations SET source_sequence=COALESCE(source_sequence,0)+1')
        elif failure == 'target':
            store.conn.execute("UPDATE prepared_action_targets SET provider_playlist_id_snapshot='different' WHERE role='deletion'")
        elif failure == 'step':
            store.conn.execute("UPDATE prepared_action_steps SET phase='failed',outcome='confirmed_present' WHERE operation='delete_playlist'")
        elif failure == 'placement':
            store.conn.execute("UPDATE prepared_action_placement_items SET video_id_snapshot='different' WHERE position=0")
        elif failure == 'resolution':
            store.conn.execute("UPDATE prepared_action_resolutions SET captured_identity_key='different',unresolved_key='different'")
        elif failure == 'extra':
            store.conn.execute('INSERT INTO prepared_action_placement_items SELECT source,preparation_revision,999,video_id_snapshot '
                'FROM prepared_action_placement_items WHERE position=0')
        elif failure == 'omitted':
            store.conn.execute('DELETE FROM prepared_action_heads WHERE source=?', (str(identities[1]),))
        else:
            filename = (header['plan_filename'] if failure == 'plan' else header['recovery_filename'] if failure == 'recovery'
                else rows(store.conn, identities[0], 'parts', 'position')[0]['filename'])
            Path(filename).write_bytes(b'corrupt')
    selected = publication.OWNER.step(store.conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        publication.OWNER.publish(store.conn, stage=selected)
    assert publication.OWNER.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_real_reconciliation_is_readback_only_and_continuing_writes_withdraw_selection(installed):
    store, _ = installed
    account, _, identities = seed(store, count=8192)
    selected = stage(store.conn)
    publication.OWNER.publish(store.conn, stage=selected)
    class PresentClient:
        def get_playlist(self, remote, limit=None):
            assert remote == 'SOURCE'
            return {'id':remote, 'tracks':[]}
        def delete_playlist(self, remote):
            pytest.fail('Historical pending deletion must not be replayed')
    with store.read_snapshot() as snapshot:
        assert len(json.loads(snapshot.store.actions.get_action(identities[1]).plan_json)['video_ids']) == 8192
        updated = playlist_deletion.reconcile_deletion(store, identities[0], {account:PresentClient()}, 100)
        assert updated.status == 'delete_failed'
        assert not snapshot.publish(lambda:None)
    assert publication.OWNER.selected(store.conn) is None
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    assert store.actions.get_failed_deletion(account, 'SOURCE').id == identities[0]
    assert publication.OWNER.value(store.conn, selected, str(identities[0]))['status'] == 'delete_pending'
    store.actions.update_action(identities[1], 'cancelled', 101)
    assert publication.OWNER.selected(store.conn) is None
    store.actions.delete_action(identities[2])
    new_id = store.actions.record_action('remove_track', '{}', '{}', 'executed', '{}', 102)
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    assert new_id > max(identities)
    assert store.actions.get_action(identities[2]) is None
    assert store.actions.get_action(identities[1]).status == 'cancelled'
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM prepared_action_parts')


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_backup_changes_reject_publication_missing_original_uses_exact_custody(installed):
    store, _ = installed
    _, path, identities = seed(store)
    selected = stage(store.conn)
    original = path.read_bytes()
    path.write_bytes(original+b' ')
    with pytest.raises(MigrationError, match='external backup changed'):
        publication.OWNER.publish(store.conn, stage=selected)
    path.unlink()
    publication.OWNER.publish(store.conn, stage=selected)
    undo = json.loads(store.actions.get_action(identities[0]).undo_json)
    assert action_backup_files.verified(undo['backups'][0])[0] == original
    path.write_bytes(original)
    Path(undo['backups'][0]).unlink()
    with pytest.raises(FileNotFoundError):
        publication.OWNER.publish(store.conn, stage=selected)
    publication.repair(store.conn, str(identities[0]))
    publication.OWNER.publish(store.conn, stage=selected)
    path.unlink()
    Path(undo['backups'][0]).unlink()
    with pytest.raises(FileNotFoundError):
        publication.repair(store.conn, str(identities[0]))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_sequence_and_reference_races_are_fenced_without_reassigning_ids(installed):
    store, _ = installed
    _, _, identities = seed(store)
    selected = stage(store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    changed = False
    def race(conn, header):
        nonlocal changed
        publication.verify_document(conn, header)
        if not changed:
            changed = True
            with other:
                other.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('actions',9999)")
    try:
        with pytest.raises(MigrationError, match='changed'):
            replace(publication.OWNER, verify_document=race).publish(store.conn, stage=selected)
    finally:
        other.close()
    publication.OWNER.publish(store.conn, stage=selected)
    assert store.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()[0] == 9999
    new_id = store.actions.record_action('undo', '{"undid":99999}', '{}', 'executed', '{}', 3)
    # Pre98 actions use INTEGER PRIMARY KEY rather than AUTOINCREMENT. Keep
    # their allocator unchanged, including an observed legacy sequence row.
    assert new_id == max(identities)+1 and publication.OWNER.selected(store.conn) is None
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    assert preparation.head(store.conn, new_id)['unresolved_undo_id'] == 99999
    assert not store.conn.execute('SELECT 1 FROM main.actions WHERE id=99999').fetchone()
    store.accounts.upsert_account('New observed', 'new.json', None, False)
    assert publication.OWNER.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_bounded_maps_and_interrupted_cutover_resume_without_losing_archives(installed):
    store, _ = installed
    for index in range(259):
        store.actions.record_action('remove_track', '{}', '{}', 'executed', '{}', index)
    preparation.JOB.install(store.conn)
    # One original part per action; use a larger bounded-fixture iteration limit.
    for _ in range(270):
        if preparation.JOB.step(store.conn).status == 'prepared':
            break
    first = publication.OWNER.step(store.conn)
    assert first.copied == 128 and not first.complete
    second = publication.OWNER.step(store.conn)
    assert second.id == first.id and second.copied == 256 and not second.complete
    completed = publication.OWNER.step(store.conn)
    assert completed.complete and completed.copied == 259
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_action_cutover BEFORE INSERT ON '+publication.OWNER.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover'):
        publication.OWNER.publish(store.conn, stage=completed.id)
    assert not store.conn.execute('SELECT 1 FROM '+publication.OWNER.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_action_cutover')
    publication.OWNER.publish(store.conn, stage=completed.id)
    assert [a.id for a in store.actions.get_actions()] == list(range(259, 0, -1))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_files_and_failed_job_do_not_hold_regular_ddl_and_retry_is_explicit(installed, monkeypatch):
    store, _ = installed
    _, _, identities = seed(store)
    selected = stage(store.conn)
    header = preparation.head(store.conn, identities[0])
    for key in ('recovery_filename','plan_filename'):
        Path(header[key]).unlink()
    Path(rows(store.conn, identities[0], 'parts', 'position')[0]['filename']).unlink()
    with pytest.raises(FileNotFoundError):
        publication.OWNER.publish(store.conn, stage=selected)
    regular.migrate(store.conn)
    publication.repair(store.conn, str(identities[0]))
    publication.OWNER.publish(store.conn, stage=selected)
    store.actions.update_action(identities[0], 'delete_failed', 5)
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled action file failure')
    publish = preparation.policy_documents._publish_encoded
    monkeypatch.setattr(preparation.policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='controlled action'):
        preparation.JOB.step(store.conn)
    progress = preparation.JOB.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_action_publication(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_action_publication'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1, 'independent_action_publication', install, validate)))
    regular.migrate(store.conn)
    assert preparation.JOB.progress(store.conn) == progress
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(store.conn)
    monkeypatch.setattr(preparation.policy_documents, '_publish_encoded', publish)
    preparation.JOB.step(store.conn, retry=True)
    publication.OWNER.publish(store.conn, stage=stage(store.conn))


def test_frozen_native_owner_wins_and_retained_map_guards_survive(tmp_path):
    path = tmp_path / 'upgrade.db'
    store = Store(path)
    try:
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:93])
        store.init_schema(defer_metadata=True)
        _, _, identities = seed(store)
        selected = stage(store.conn)
        publication.OWNER.publish(store.conn, stage=selected)
        originals = {identity:publication.OWNER.value(store.conn, selected, str(identity)) for identity in identities}
        # The frozen one-time command owns a dedicated connection, without
        # serving TEMP compatibility views shadowing replaced physical tables.
        store.conn.close()
        store = Store(path)
        runner.migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema(defer_metadata=True)
        assert action_work.available(store.conn)
        assert publication.OWNER.selected(store.conn) is None
        for identity, original in originals.items():
            assert publication.OWNER.value(store.conn, selected, str(identity)) == original
            assert store.actions.get_action(identity).id == identity
        regular.validate(store.conn)
        with store.conn:
            store.conn.execute('DROP TRIGGER prepared_action_parts_mapped_delete')
        with pytest.raises(MigrationError, match='publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_duplicate_draft_signatures_cannot_publish_an_ambiguous_owner(installed):
    store, _ = installed
    seed(store)
    store.actions.record_action('merge_draft', '{}', 'SOURCE|COPY', 'draft', 'plain evidence', 0)
    selected = stage(store.conn)
    with pytest.raises(MigrationError, match='duplicate draft signatures'):
        publication.OWNER.publish(store.conn, stage=selected)
    assert publication.OWNER.selected(store.conn) is None
    assert len(store.actions.get_actions()) == 6


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_sequence_change_after_full_verification_is_rejected_under_final_writer(installed, monkeypatch):
    store, _ = installed
    selected = stage(store.conn)
    verify = publication.ActionPublication.verify
    other = sqlite3.connect(store._db_path)
    def race(owner, conn, identity):
        captured = verify(owner, conn, identity)
        with other:
            other.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('actions',8888)")
        return captured
    try:
        monkeypatch.setattr(publication.ActionPublication, 'verify', race)
        with pytest.raises(MigrationError, match='changed after verification'):
            publication.OWNER.publish(store.conn, stage=selected)
        assert not store.conn.execute('SELECT 1 FROM '+publication.OWNER.table('generations')).fetchone()
        assert store.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()[0] == 8888
    finally:
        other.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_backup_custody_survives_unrelated_writes_and_crash_after_undo_claim(installed):
    store, _ = installed
    _, path, identities = seed(store)
    action = identities[0]
    raw = json.dumps({'backups':[str(path)], 'paid':{'unchanged':'東京'}})
    store.actions.update_action(action, 'executed', 5, undo_json=raw)
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    retained = json.loads(store.actions.get_action(action).undo_json)['backups'][0]
    path.unlink()
    store.actions.record_action('remove_track', '{}', '{}', 'executed', '{}', 6)
    assert publication.OWNER.selected(store.conn) is None
    claimed = store.actions.claim_undo(action, 7)
    assert json.loads(claimed.undo_json)['backups'] == [retained]
    # The legacy claim changes status, before any progress document is saved.
    assert store.conn.execute('SELECT undo_json FROM main.actions WHERE id=?', (action,)).fetchone()[0] == raw
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        resumed = reopened.actions.get_action(action)
        assert resumed.status == 'undoing' and resumed.executed_at == 7
        assert json.loads(resumed.undo_json) == {'backups':[retained], 'paid':{'unchanged':'東京'}}
        assert Path(retained).is_file()
        changed = json.dumps({'backups':[str(path)], 'paid':{'changed':True}})
        reopened.actions.update_action(action, 'undo_failed', 8, undo_json=changed)
        assert reopened.actions.get_action(action).undo_json == changed
    finally:
        reopened.conn.close()
