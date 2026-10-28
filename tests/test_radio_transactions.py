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
from tests.radio_session_support import poc, play
from tests.conftest import FakeClient
from yt_playlist.library import playlist_generated
from yt_playlist.library.playlist_generated import create_generated_playlist


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


def test_radio_creation_finishing_after_repair_retains_only_source_provenance(poc, monkeypatch):
    c, _, frames, _, pool = poc
    store = c.ctx.store
    iid = store.identities.get_master_identity().id
    remote = FakeClient(catalog={t['video_id']: {'videoId': t['video_id'], 'title': t['title'],
        'artists': [{'name': t['artist']}], 'duration_seconds': 180} for t in pool})
    c.ctx.client_provider = lambda: {iid: remote}
    monkeypatch.setattr(playlist_generated, 'create_generated_playlist', create_generated_playlist)
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
        assert json.loads(store.settings.get_setting('radio_session_playlist_ids')) == [result['playlist_id']]
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False


@pytest.mark.parametrize('replacement', [False, True])
def test_shared_protocol_journals_before_transport_and_does_not_replay_lost_reply(replacement):
    saved = {SESSION_KEY: json.dumps({'version': 2, 'phase': 'playing', 'episode_id': 'episode'})}
    settings = SimpleNamespace(get_setting=saved.get, set_setting=saved.__setitem__)
    persistence = RadioPersistence(settings)
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
    persistence = RadioPersistence(SimpleNamespace(get_setting=lambda _key: state))
    owner = RadioMutations(None, persistence, None, lambda: 0.)
    operation = AppendOperation({'video_id': 'a'})
    edits = RadioEdits()
    edits.begin(operation)
    target = PlaylistTarget(None, 'playlist', 1, 'Radio', 'older', threading.Event())
    with pytest.raises(ValueError, match='superseded session'):
        owner._send(target, edits, operation, PlaylistEdit('add', ('a',), track=operation.track))
