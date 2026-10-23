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
