"""Real predecessor conversion and controlled interrupted deletion transports."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from tests.core.test_action_work_migration import legacy_account, legacy_playlist
from tests.test_undo_recovery import RecoveryClient
from yt_playlist.core import action_work, fao
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.preparation.deletion_readback import JOB
from yt_playlist.core.store import Store
from yt_playlist.library import executor, playlist_deletion
from yt_playlist.repos.models import Playlist


class InterruptedDeletion(RecoveryClient):
    def __init__(self):
        super().__init__({'SOURCE':['a','b'],'KEEP':['a','b']})
        self.attempts = []
        self.reads = []

    def delete_playlist(self, playlistId):
        self.attempts.append(playlistId)
        raise SystemExit('interrupted after transport invocation')

    def get_playlist(self, playlistId, limit=None):
        self.reads.append((playlistId,limit))
        return super().get_playlist(playlistId,limit)


def _step(store, action):
    row, = store.conn.execute('SELECT * FROM action_steps WHERE action_id=?',(action,)).fetchall()
    return row


def _writes(client):
    return (list(client.attempts),list(client.added),list(client.removed),list(client.created))


def _merge(store, account, memberships, client, now):
    executor.apply_result(store,{account:client},memberships,['a','b','c'],'all',now)


@pytest.mark.parametrize('already_converted',[False,True],ids=['pre98','converted98'])
@pytest.mark.parametrize('remote_present',[True,False],ids=['present','absent'])
def test_imported_pending_requires_readback_before_keeper_writes(tmp_path,monkeypatch,
                                                               already_converted,remote_present,
                                                               _pristine_installed_schema):
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path/'home'))
    path = tmp_path/'actions.db'
    store = Store(path)
    try:
        # Replay the empty historical prefix once; every case still publishes
        # its own real backup, interrupted intent and optional buggy cutover.
        _pristine_installed_schema(97).backup(store.conn)
        store._bind_repositories()
        account = legacy_account(store)
        memberships = [legacy_playlist(store,account,remote) for remote in ('SOURCE','KEEP')]
        client = InterruptedDeletion()

        # Only the predecessor read adapter translates v97's physical names;
        # backup, journal publication and transport use production deletion code.
        def predecessor_playlist(identity):
            row = store.conn.execute('SELECT * FROM playlists WHERE id=?',(identity,)).fetchone()
            if row is None:
                return None
            values = dict(row)
            values['account_id'] = values.pop('identity_id')
            values['provider_playlist_id'] = values.pop('ytm_playlist_id')
            return Playlist(**{name:values[name] for name in Playlist.__dataclass_fields__})

        def predecessor_groups():
            return dict(store.conn.execute('SELECT ref,group_name FROM playlist_scopes WHERE group_name IS NOT NULL'))

        def predecessor_provenance(remote):
            row = store.conn.execute('SELECT origin,adopted,provenance_description description FROM playlist_scopes '
                                     'WHERE provider_id=? AND origin IS NOT NULL',(remote,)).fetchone()
            return dict(row) if row else {'origin':'ordinary','adopted':1,'description':None}

        with monkeypatch.context() as patch:
            patch.setattr(store.playlists,'get_playlist',predecessor_playlist)
            patch.setattr(store.playlists,'get_playlist_groups',predecessor_groups)
            patch.setattr(store.playlists,'playlist_provenance',predecessor_provenance)
            with pytest.raises(SystemExit,match='after transport invocation'):
                executor.delete_playlist(store,memberships[0],client,2)
        action, = store.actions.get_actions()
        entry, = json.loads(action.undo_json)['deletions']
        assert entry['phase']=='pending' and client.attempts==['SOURCE']
        backup = fao.read(entry['backup']).bytes
        if already_converted:
            # Recreate the installed v98 bug using its actual conversion/runner.
            original = action_work._step
            def old_conversion(*args,**kwargs):
                kwargs['legacy'] = False
                return original(*args,**kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(action_work,'_step',old_conversion)
                patch.setattr(action_work,'imported_pending',lambda *args: False)
                migrate(store.conn,registry=registry.MIGRATIONS[:98])
            assert (_step(store,action.id)['phase'],_step(store,action.id)['outcome'])==('pending','not_sent')
        store.conn.close()
        store = Store(path)
        store.init_schema()
        store.conn.close()
        store = Store(path)
        store.init_schema()
        version = 98 if already_converted else 97
        assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == version
        if already_converted:
            step = _step(store,action.id)
            assert (step['phase'],step['outcome'])==('pending','not_sent')
            assert step['backup_status']=='verified'
            assert step['backup_digest']==hashlib.sha256(backup).hexdigest()
            assert fao.read(step['backup_filename']).bytes==backup
            assert json.loads(store.actions.get_action(action.id).undo_json)['deletions'][0]['phase']=='uncertain'
            JOB.install(store.conn)
            prepared = JOB.step(store.conn, batch_size=1)
            assert prepared.processed == 1
            while prepared.status != 'prepared':
                prepared = JOB.step(store.conn, batch_size=1)
            assert tuple(store.conn.execute('SELECT requires_readback,effective_phase FROM prepared_deletion_readback').fetchone()) == (1, 'uncertain')
            original, = store.conn.execute('SELECT source_snapshot FROM prepared_deletion_originals').fetchone()
            assert json.loads(original)['step']['outcome'] == 'not_sent'
        else:
            assert fao.read(entry['backup']).bytes == backup
        assert store.actions.get_action(action.id).status=='delete_pending'
        if not remote_present:
            del client._tracks['SOURCE']
        before = _writes(client)
        # An absent keeper can fail its preliminary complete read before target
        # claims are acquired. Both paths must reject without any provider write.
        with pytest.raises((ValueError,YTMusicServerError),match='unknown.*reconcile|404'):
            _merge(store,account,memberships,client,3)
        assert _writes(client)==before

        reads_before = len(client.reads)
        resolved = playlist_deletion.reconcile_deletion(store,action.id,{account:client},4)
        assert ('SOURCE',None) in client.reads[reads_before:]
        assert _writes(client)==before
        assert resolved.status==('delete_failed' if remote_present else 'executed')
        if already_converted:
            assert _step(store,action.id)['outcome']==('confirmed_present' if remote_present else 'complete')
            assert JOB.progress(store.conn).prepared_revision is None
            prepared = JOB.step(store.conn, batch_size=1)
            while prepared.status != 'prepared':
                prepared = JOB.step(store.conn, batch_size=1)
            assert store.conn.execute('SELECT requires_readback FROM prepared_deletion_readback').fetchone()[0] == 0
            assert store.conn.execute('SELECT COUNT(*) FROM prepared_deletion_originals').fetchone()[0] > 1
        assert (store.playlists.get_playlist(memberships[0]) is not None)==remote_present
        if remote_present:
            _merge(store,account,memberships,client,5)
            assert [[row['videoId'] for row in client._tracks[remote]] for remote in ('SOURCE','KEEP')]==[
                ['a','b','c'],['a','b','c']]
        assert client.attempts==['SOURCE']
        from yt_playlist.core.migrations.runner import validate_version
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as reader:
            reader.row_factory = sqlite3.Row
            validate_version(reader, version)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_current_unsent_intent_survives_repair_and_admits_keeper_write(tmp_path,monkeypatch,
                                                                  _pristine_installed_schema):
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path/'home'))
    path = tmp_path/'unsent.db'
    store = Store(path)
    try:
        _pristine_installed_schema(106).backup(store.conn)
        store._bind_repositories()
        account = store.accounts.upsert_account('Owner','owner.json',None,True)
        memberships = [store.playlists.upsert_playlist(account,remote,remote,2,'',1)
                       for remote in ('SOURCE','KEEP')]
        client = InterruptedDeletion()
        def before_admission(*args,**kwargs):
            raise SystemExit('before admission')
        with monkeypatch.context() as patch:
            patch.setattr(store.actions,'begin_deletion',before_admission)
            with pytest.raises(SystemExit,match='before admission'):
                executor.delete_playlist(store,memberships[0],client,2)
        action, = store.actions.get_actions()
        step = dict(_step(store,action.id))
        assert (step['phase'],step['outcome'])==('pending','not_sent')
        assert not client.attempts
        store.conn.close()
        store = Store(path)
        store.init_schema()
        assert dict(_step(store,action.id))==step
        JOB.install(store.conn)
        assert JOB.step(store.conn).status == 'prepared'
        assert store.conn.execute('SELECT requires_readback FROM prepared_deletion_readback').fetchone()[0] == 0
        _merge(store,account,memberships,client,3)
        assert not client.attempts
        assert [[row['videoId'] for row in client._tracks[remote]] for remote in ('SOURCE','KEEP')]==[
            ['a','b','c'],['a','b','c']]
    finally:
        store.conn.close()
