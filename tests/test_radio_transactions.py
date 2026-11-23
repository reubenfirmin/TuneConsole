"""Shared transaction protocol: durable intent, lost replies and concurrent user intent."""
import json
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.web.radio_edits import AppendOperation, RadioEdits
from yt_playlist.web.radio_mutations import PlaylistTarget, RadioMutations
from yt_playlist.web.radio_persistence import RadioPersistence, SESSION_KEY
from yt_playlist.web.radio_playlist_io import PlaylistEdit, RadioPlaylistIO
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail, TailView
from yt_playlist.web.radio_state import Phase
from tests.radio_session_support import (
    uncontended_poc as poc, completed_radio_schema, completed_radio_store, play,
)
from tests.conftest import FakeClient
from yt_playlist.core.recommendation_custody import ROOTS as RECOMMENDATION_ROOTS
from yt_playlist.library import generated_acquisition
from yt_playlist.library.generated_acquisition import create_generated_playlist


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Transaction controllers never construct the HTTP application."""


def test_existing_radio_keeps_running_with_discarded_writes_and_resumes_saving(poc):
    c, remote, *_ = poc
    assert c.start()['ok']
    store, session = c.ctx.store, c.session
    checkpoint = store.settings.get_setting(SESSION_KEY)
    store.activity_paused = True
    store.conn.execute('PRAGMA query_only=ON')
    try:
        play(c, 'v3')
        assert remote.ids == ['v1', 'v2', 'v3', 'v4']
        assert len(c.persistence.load()['tracks']) == 4
        assert store.settings.get_setting(SESSION_KEY) == checkpoint
        assert len(store.charts.playlist_tracks_detail(session.local_id)) == 3
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
    play(c, 'v4')
    assert remote.ids == ['v1', 'v2', 'v3', 'v4', 'v5']
    assert len(c.persistence.load()['tracks']) == 5
    assert len(store.charts.playlist_tracks_detail(session.local_id)) == 5
    assert c.persistence.transient is None


def test_prepared_radio_starts_readonly_then_retains_its_publication_on_resume(poc, monkeypatch):
    from tests.test_radio_preparation import prepare_radio_track_groups

    c, _, frames, _, pool = poc
    store = c.ctx.store
    prepare_radio_track_groups(c, pool, monkeypatch)
    account = store.accounts.get_master_account().id
    provider = FakeClient(catalog={track['video_id']: {
        'videoId': track['video_id'], 'title': track['title'],
        'artists': [{'name': track['artist']}], 'duration_seconds': 180} for track in pool})
    c.ctx.client_provider = lambda: {account: provider}
    monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create_generated_playlist)
    generation = store.recommendations.head('home')['id']
    store.activity_paused = True
    store.conn.execute('PRAGMA query_only=ON')
    changes = store.conn.total_changes
    try:
        result = c.start(seed_video_id='v1')
        assert result['ok'], result.get('reason')
        assert result['local_id'] is None
        assert len(provider.created) == len(frames) == 1
        assert c.session.candidates.prepared is None
        assert not provider.added
        play(c, 'v1', playlist=result['playlist_id'])
        assert c.session.candidates.prepared.generation == generation
        assert store.conn.total_changes == changes
        assert not store.conn.execute(f"SELECT 1 FROM {RECOMMENDATION_ROOTS} AND root.value->>0='pending'").fetchone()
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
    try:
        play(c, 'v1', playlist=result['playlist_id'])
        play(c, 'v1', playlist=result['playlist_id'])
        assert [row[0] for row in store.conn.execute(
            f"SELECT g.id FROM {RECOMMENDATION_ROOTS} AND root.value->>0='pending'")] == [generation]
        assert c.session.candidates.prepared._lifetime.alive
        assert len(provider.created) == len(frames) == 1
    finally:
        c.session.candidates.close()


@pytest.mark.parametrize('thin_group', [False, True], ids=['full-leaf', 'singleton-leaf'])
def test_prepared_radio_records_the_click_before_playback(poc, monkeypatch, thin_group):
    from tests.recommendation_support import publish_bundles
    from tests.test_radio_preparation import prepare_radio_track_groups
    from yt_playlist.rec import choice_evidence

    radio, _, frames, _, pool = poc
    store = radio.ctx.store
    prepare_radio_track_groups(radio, pool, monkeypatch)
    generation = store.taste_groups.head_generation()
    members = {}
    leaves = store.taste_groups.leaf_members(generation)
    for key, group in leaves.items():
        members.setdefault(group, []).append(key)
    bundle = dict(store._fixture_bundle_payload)
    if thin_group:
        seed = pool[0]['key']
        members[leaves[seed]] = [seed]
        bundle['all'] = {'wheelhouse': pool[8:16]}
    publish_bundles(store, {**bundle,
        '_radio': {'generation': generation, 'members': members}}, 1234)

    video = pool[0]['video_id']
    menu = choice_evidence.issue(store, 'radio', [{'id': video, 'tracks': [pool[0]]}], 1200, context={})
    assert choice_evidence.respond(store, menu, 'visible', 1201, visible=[video], synchronous=True)
    with radio.launch_request('prepared-click') as launch:
        monkeypatch.setattr(radio.ctx, 'now_fn', lambda: 1250.)
        assert radio.start(seed_video_id=video, menu_id=menu, launch=launch)['ok']
    assert radio.session.seeded_launch
    assert len(frames) == 1 and radio.ctx.bridge.now_playing is None
    assert [track['video_id'] for track in radio.session.selected] == [video]

    def responses():
        return [(row['kind'], row.get('option_id'), row['at'])
                for row in store.rec_ledger.evidence().records('response')
                if row['opportunity_id'] == menu and row['kind'] != 'experience']

    click = [('visible', None, 1201), ('choice', video, 1234)]
    assert responses() == click
    monkeypatch.setattr(radio.ctx, 'now_fn', lambda: 1264.)
    play(radio, video)
    assert responses() == [*click, ('start', video, 1264)]


@pytest.mark.parametrize('path', ['selected', 'saved', 'empty-readback', 'cancelled', 'rollback'])
def test_radio_launch_reuses_one_readback_for_playback_and_undo(poc, monkeypatch, path):
    c, _, frames, _, pool = poc
    store = c.ctx.store
    account = store.accounts.get_master_account().id
    reads = []

    class Provider(FakeClient):
        def get_playlist(self, playlistId, limit=100):
            reads.append((playlistId, limit))
            detail = super().get_playlist(playlistId, limit=limit)
            _, title, description = self.created[-1]
            if path == 'empty-readback':
                with store.read_view() as reader:
                    assert reader.playlists.get_playlist_for_account(account, playlistId) is None
                    assert not reader.actions.get_actions()
                if len(reads) == 1:
                    detail = {**detail, 'trackCount': 0, 'tracks': []}
            return {**detail, 'title': title, 'description': description}

    remote = Provider(catalog={t['video_id']: {'videoId': t['video_id'], 'title': t['title'],
        'artists': [{'name': t['artist']}], 'duration_seconds': 180,
        'setVideoId': 'handle-' + t['video_id']} for t in pool})
    c.ctx.client_provider = lambda: {account: remote}
    monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create_generated_playlist)

    source = None
    if path != 'selected':
        source = store.playlists.upsert_playlist(account, 'SOURCE', 'Source playlist', 3, '', 1234.)
        ids = store.tracks.track_ids_for_videos(['v1', 'v2', 'v3'])
        store.playlists.set_playlist_tracks(source, [ids[video] for video in ('v1', 'v2', 'v3')])
        monkeypatch.setattr(c.work, 'submit', lambda *args, **kwargs: None)
    if path == 'cancelled':
        add = remote.add_playlist_items

        def cancel_after_add(*args, **kwargs):
            result = add(*args, **kwargs)
            assert c.stop(launch_id='browser-launch')['ok']
            return result

        monkeypatch.setattr(remote, 'add_playlist_items', cancel_after_add)
    if path == 'rollback':
        publish = store.radio_checkpoints.repository.publish
        attempts = []

        def fail_once(state, *args, **kwargs):
            attempts.append(state['episode_id'])
            if len(attempts) == 1:
                raise ValueError('injected checkpoint failure')
            with store.read_view() as reader:
                assert not reader.playlists.get_playlist_for_account(account, state['playlist'])
                assert not reader.actions.get_actions()
                assert reader.radio_checkpoints.load() == (None, None)
            return publish(state, *args, **kwargs)

        monkeypatch.setattr(store.radio_checkpoints.repository, 'publish', fail_once)

    with c.launch_request('browser-launch') as launch:
        result = c.start(seed_video_id='v1' if source is None else None, playlist_id=source, launch=launch)
    assert result['ok'] == (path not in ('cancelled', 'rollback')), result
    if not result['ok']:
        result = c.status()
    playlist = result['playlist_id']
    if path == 'selected':
        assert not remote.added and not reads
        assert [track['video_id'] for track in result['tracks']] == ['v1']
        assert not result['directions'] and result['local_id'] is None
        assert not store.actions.get_actions()
        play(c, 'v1', playlist=playlist, paused=True)
        assert not remote.added and not reads
        play(c, 'v1', playlist=playlist)
    state = RadioPersistence(store.radio_checkpoints).load()
    videos = [track['video_id'] for track in state['tracks'] or state['initial_tracks']]
    assert len(videos) == 3 and videos[0] == 'v1'
    if path == 'selected':
        assert remote.added == [(playlist, [video]) for video in videos[1:]]
        assert reads == [(playlist, None)] * 4
    else:
        assert remote.added == [(playlist, videos)]
        assert reads == [(playlist, None)] * (2 if path == 'empty-readback' else 1)
    assert len(frames) == int(path not in ('cancelled', 'rollback'))
    assert state['local_id'] == c.session.local_id
    if source is not None:
        assert state['local_id'] != source
    assert [row['video_id'] for row in store.charts.playlist_tracks_detail(state['local_id'])] == videos
    action = store.actions.get_actions()[0]
    undo = json.loads(action.undo_json)
    assert undo['new_ytm'] == playlist
    assert undo['post']['videos'] == videos
    assert undo['post']['handles'] == ['handle-' + video for video in videos]
    assert store.playlists.is_quarantined_playlist(playlist)
    assert playlist in store.playlists.radio_playlist_ids()
    assert RadioPersistence(store.radio_checkpoints).launch_receipts()['browser-launch'] == state['episode_id']
    with c.launch_request('browser-launch') as launch:
        assert launch.replay or path == 'cancelled' and launch.cancelled.is_set()
        assert c.start(playlist_id=source, launch=launch)['ok'] == (path in ('selected', 'saved', 'empty-readback'))
    assert len(remote.created) == len(store.actions.get_actions()) == 1
    assert len(remote.added) == (2 if path == 'selected' else 1)


def test_radio_creation_finishing_after_repair_retains_only_source_provenance(poc, monkeypatch):
    c, _, frames, _, pool = poc
    store = c.ctx.store
    iid = store.accounts.get_master_account().id
    remote = FakeClient(catalog={t['video_id']: {'videoId': t['video_id'], 'title': t['title'],
        'artists': [{'name': t['artist']}], 'duration_seconds': 180} for t in pool})
    c.ctx.client_provider = lambda: {iid: remote}
    monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create_generated_playlist)
    add = remote.add_playlist_items

    def finish_repair_then_add(*args, **kwargs):
        # The creator already chose to discard its local materialization.
        # Repair finishes while the remote write is still in flight.
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
        c.launch.retain_provenance()
        return add(*args, **kwargs)

    monkeypatch.setattr(remote, 'add_playlist_items', finish_repair_then_add)
    store.activity_paused = True
    store.conn.execute('PRAGMA query_only=ON')
    try:
        result = c.start()
        assert result['ok'], result
        assert result['local_id'] is None and frames
        assert store.settings.get_setting(SESSION_KEY) is None
        assert store.playlists.is_quarantined_playlist(result['playlist_id'])
        from yt_playlist.core import radio_provenance
        assert radio_provenance.ids(store.conn) == [result['playlist_id']]
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False


@pytest.mark.parametrize('replacement', [False, True])
def test_shared_protocol_journals_before_transport_and_does_not_replay_lost_reply(replacement):
    saved = {SESSION_KEY: json.dumps({'version': 2, 'phase': 'playing', 'episode_id': 'episode'})}
    settings = SimpleNamespace(get_setting=saved.get, set_setting=saved.__setitem__)
    checkpoints = SimpleNamespace(load=lambda: (json.loads(saved[SESSION_KEY]), None),
        save=lambda encoded, **_kw: settings.set_setting(SESSION_KEY, encoded))
    persistence = RadioPersistence(checkpoints)
    before, added = {'video_id': 'a'}, {'video_id': 'b'}
    edit = PlaylistEdit('add', ('a', 'b'), track=added)
    operation = RadioPlaylistTail([before], [added], 1) if replacement else AppendOperation(added)
    edits = RadioEdits()
    edits.begin(operation)
    calls, published = [], []
    rows = [{'videoId': 'a', 'setVideoId': 'set-a'}]
    target = PlaylistTarget(None, 'playlist', 1, 'Radio', 'episode', threading.Event())

    def add(_playlist, videos):
        state = persistence.load()
        assert state['replacement']['waiting'] == ['a', 'b'] if replacement else state['pending_sent']
        calls.append(videos)
        rows.append({'videoId': 'b', 'setVideoId': 'set-b'})
        # A Stop and later queued choice are saved while the transport is busy.
        state.update(phase=Phase.STOPPING, stopped=True,
                     requested_direction={'slot': 2, 'tracks': [added], 'choice': None})
        settings.set_setting(SESSION_KEY, json.dumps(state))
        target.stop.set()
        raise RuntimeError('reply lost after the write committed')

    target = PlaylistTarget(SimpleNamespace(get_playlist=lambda *_a, **_kw: {
        'tracks': list(rows), 'trackCount': len(rows)}, add_playlist_items=add),
        target.playlist, target.local_id, target.title, target.episode_id, target.stop)
    io = RadioPlaylistIO(SimpleNamespace(publish_confirmed_tracks=lambda *a, **kw: published.append(a)))
    owner = RadioMutations(io, persistence, None, lambda: 10.)
    view = TailView((before,), 0, 0, frozenset(), {'a': before, 'b': added})
    result = (owner.edit_tail(target, edits, view, edit, tuple(rows)) if replacement
              else owner.append(target, edits, view))
    assert result.status == 'confirmed'
    assert [t['video_id'] for t in result.tracks] == ['a', 'b']
    assert calls == [['b']] and len(published) == 1
    state = persistence.load()
    assert state['phase'] == Phase.STOPPING and state['stopped']
    assert state['requested_direction']['slot'] == 2
    # Another complete read still cannot dispatch the same write again.
    if replacement:
        owner.read_tail(target, operation, view)
    else:
        owner.append(target, edits, view)
    assert calls == [['b']]


def test_operation_owner_rejects_overlap_and_stale_completion():
    edits = RadioEdits()
    append = AppendOperation({'video_id': 'a'})
    tail = RadioPlaylistTail([], [], 0)
    edits.begin(append)
    with pytest.raises(ValueError, match='Resolve the active'):
        edits.begin(tail)
    edits.queue({'slot': 1, 'tracks': [], 'choice': None})
    edits.queue({'slot': 2, 'tracks': [], 'choice': None})
    assert edits.active is append and edits.requested['slot'] == 2
    edits.finish(append)
    edits.begin(tail)
    with pytest.raises(ValueError, match='superseded'):
        edits.finish(append)
    assert edits.active is tail


def test_superseded_episode_cannot_send_a_write():
    state = json.dumps({'version': 2, 'phase': 'playing', 'episode_id': 'newer'})
    persistence = RadioPersistence(SimpleNamespace(load=lambda: (json.loads(state), None)))
    owner = RadioMutations(None, persistence, None, lambda: 0.)
    operation = AppendOperation({'video_id': 'a'})
    edits = RadioEdits()
    edits.begin(operation)
    target = PlaylistTarget(None, 'playlist', 1, 'Radio', 'older', threading.Event())
    with pytest.raises(ValueError, match='superseded session'):
        owner._send(target, edits, operation, PlaylistEdit('add', ('a',), track=operation.track))
