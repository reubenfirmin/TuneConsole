import json
from pathlib import Path

import pytest

from tests.test_undo_recovery import RecoveryClient
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.library import executor, playlist_undo, sync
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, PROMOTED_DESCRIPTION


def test_group_names_cannot_adopt_or_quarantine_and_stale_sync_cannot_revoke_adoption(store):
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    pid = store.playlists.upsert_playlist(iid, 'PL', 'Mix', 0, '', 1)
    store.playlists.set_playlist_group('PL', 'Generated')
    assert store.rec_query.excluded_playlist_ids() == set()
    store.playlists.mark_generated_playlist('PL')
    store.playlists.set_playlist_group('PL', 'Road trips')
    assert store.rec_query.excluded_playlist_ids() == {pid}
    store.playlists.reconcile_playlist_provenance('PL', None)
    assert store.rec_query.excluded_playlist_ids() == {pid}
    store.history.record_play_event(iid, 'song', 'video', 10, playlist_ytm_id='PL', source_timezone='UTC')
    store.playlists.adopt_generated_playlist('PL')
    store.playlists.reconcile_playlist_provenance('PL', GENERATED_DESCRIPTION)
    assert store.rec_query.excluded_playlist_ids() == set()
    assert store.playlists.get_playlist_groups()['PL'] == 'Road trips'
    assert store.rec_query.generated_only_play_days() == {(0, 'song')}
    assert store.trends.ledger_day_counts() == []


@pytest.mark.parametrize('generated,adopted,operation', [
    (True, False, 'manual'), (True, False, 'gc'),
    (True, True, 'manual'), (False, True, 'manual')])
def test_recovery_preserves_policy_groups_and_remote_markers(store, tmp_path, monkeypatch,
                                                           generated, adopted, operation):
    monkeypatch.setenv('YT_PLAYLIST_HOME', str(tmp_path))
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    pid = store.playlists.upsert_playlist(iid, 'SOURCE', 'Mix', 1, '', 1)
    tid = store.tracks.upsert_track('a', 'A', 'Artist', None, 200)
    store.playlists.set_playlist_tracks(pid, [tid])
    store.playlists.set_playlist_group('SOURCE', 'Road trips')
    client = RecoveryClient({'SOURCE': ['a']})
    if generated:
        store.playlists.mark_generated_playlist('SOURCE')
        client.metadata['SOURCE']['description'] = GENERATED_DESCRIPTION
        if adopted:
            store.playlists.adopt_generated_playlist('SOURCE')
            client.metadata['SOURCE']['description'] = PROMOTED_DESCRIPTION
    if operation == 'gc':
        assert executor.gc_generated_playlists(store, {iid: client}, 40 * 86400, grace_days=1)
    else:
        executor.delete_playlist(store, pid, client, 40 * 86400)
    action = store.actions.get_actions()[0]
    snapshot = json.loads(Path(json.loads(action.undo_json)['backup']).read_text())
    assert snapshot['snapshot_version'] == 2
    assert snapshot['group'] == 'Road trips'
    playlist_undo.undo_action(store, action.id, {iid: client}, 40 * 86400 + 1)
    new_id = client.created[0][0]
    assert store.playlists.get_playlist_groups()[new_id] == 'Road trips'
    assert store.playlists.is_quarantined_playlist(new_id) == (generated and not adopted)
    # Another install sees only remote metadata, so it must reach the same policy decision.
    other = Store(':memory:')
    other.init_schema()
    other_id = other.identities.upsert_identity('other', 'bridge', None, True)
    sync.refresh_playlist(other, other_id, client, new_id, 'Mix', 40 * 86400 + 2)
    assert other.playlists.is_quarantined_playlist(new_id) == (generated and not adopted)
    other.conn.close()


def test_populated_policy_upgrade_preserves_curation_and_is_idempotent(tmp_path):
    store = Store(tmp_path / 'state.db')
    migrate(store.conn, registry=MIGRATIONS[:18])
    store.conn.execute("INSERT INTO playlist_group VALUES ('gen','Generated')")
    store.conn.execute("INSERT INTO playlist_group VALUES ('adopted','Faves')")
    store.conn.execute("INSERT INTO rec_recipes(playlist_ytm,recipe,created_at) VALUES ('adopted','{}',1)")
    store.conn.commit()
    store.init_schema()
    assert store.playlists.is_quarantined_playlist('gen')
    assert store.playlists.playlist_provenance('adopted')['adopted'] == 1
    store.playlists.set_playlist_group('gen', 'Road trips')
    store.init_schema()
    assert store.playlists.is_quarantined_playlist('gen')
    assert store.playlists.get_playlist_groups()['gen'] == 'Road trips'
    assert len(list((tmp_path / 'backups').glob('*.db'))) == 1
