"""Stream-song choices start after confirmed addition; queue cleanup follows safely."""
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import poc, play
from yt_playlist.rec.radio_directions import DEPTH
from yt_playlist.web import radio_persistence
from yt_playlist.web.routes import radio_session as radio_routes


@pytest.mark.parametrize('slot,index', [(0, 0), (0, 2), (1, 1), (3, DEPTH - 1)])
def test_song_play_confirms_saved_target_before_navigation_and_refills_stream(poc, slot, index):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][slot]
    target = branch['tracks'][index]['video_id']
    continuation = [t['video_id'] for t in branch['tracks'][index:]]
    skipped = {t['video_id'] for t in branch['tracks'][:index]}
    heard = list(c.session.heard)
    c.ctx.bridge.auto_adopt = False
    assert c.select(branch['id'], state['menu_id'], target)['ok']
    assert len(frames) == 1 and remote.ids == ['v1', 'v2', 'v3']
    c.advance(c.session)
    assert remote.ids == ['v1', 'v2', target]
    assert len(frames) == 2, 'Confirmed saved contents must not wait for the old native queue'
    assert not c.status()['updating']
    c.ctx.bridge.native_ids = list(remote.ids)
    c.advance(c.session)
    assert len(frames) == 2
    assert frames[-1]['url'].endswith(f'v={target}&list=PLSESSION')
    assert c.session.directions.active == slot
    future = c.session.directions.slots[slot]['tracks']
    assert len(future) == DEPTH
    assert [t['video_id'] for t in future[:len(continuation)]] == continuation
    assert not skipped & {t['video_id'] for t in future}
    assert c.session.heard == heard, 'Choosing a future song must not manufacture listened history'
    c.advance(c.session)
    assert len(frames) == 2, 'Waiting for the navigation heartbeat must not repeat navigation'
    c.ctx.bridge.auto_adopt = True
    play(c, target)
    assert c.status()['ahead'] == 1
    assert remote.ids[:3] == ['v1', 'v2', target]
    assert remote.ids[3] == future[1]['video_id']


@pytest.mark.parametrize('replacement', ['another-song', 'heading', 'external-playback', 'stop'])
def test_a_later_choice_or_stop_supersedes_a_song_handoff(poc, replacement):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    first, latest = state['directions'][1:3]
    c.select(first['id'], state['menu_id'], first['tracks'][2]['video_id'])
    c.ctx.bridge.auto_adopt = False
    def supersede_during_write():
        if replacement == 'stop':
            c.stop()
        elif replacement == 'external-playback':
            c.ctx.bridge.begin_playback_intent()
        else:
            assert c.select(latest['id'], state['menu_id'],
                            latest['tracks'][1]['video_id'] if replacement == 'another-song' else None)['ok']

    remote.after_add = supersede_during_write
    c.advance(c.session)
    remote.after_add = lambda: None
    c.ctx.bridge.auto_adopt = True
    c.advance(c.session)
    if replacement == 'another-song':
        assert len(frames) == 2
        assert frames[-1]['url'].endswith(f"v={latest['tracks'][1]['video_id']}&list=PLSESSION")
    else:
        assert len(frames) == 1


@pytest.mark.parametrize('active_edit', [False, True])
def test_checkpoint_restores_queue_work_without_replaying_a_thumbnail_click(poc, active_edit):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    c.select(branch['id'], state['menu_id'], branch['tracks'][2]['video_id'])
    if active_edit:
        remote.after_add = lambda: setattr(remote, 'fail_read', True)
        with pytest.raises(RuntimeError, match='Read response lost'):
            c.advance(c.session)
        remote.after_add = lambda: None
    saved = json.loads(radio_persistence.encode(c.session))
    assert 'play' not in (saved['requested_direction'] or {})
    assert 'play' not in (saved['replacement'] or {})
    restored = radio_persistence.restore(saved, remote, c.session.identity, c.session.candidates)
    assert not restored.requested_direction or not restored.requested_direction.get('play')
    assert not restored.replacement or restored.replacement.play is None
    c.session = restored
    c.ctx.bridge.auto_adopt = True
    c.advance(restored)
    assert len(frames) == 1


def test_unavailable_explicit_song_does_not_play_a_different_song(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    add = remote.add_playlist_items

    def reject(playlist, ids):
        if ids == [target]:
            return {'status': 'STATUS_FAILED'}
        return add(playlist, ids)

    remote.add_playlist_items = reject
    assert c.select(branch['id'], state['menu_id'], target)['ok']
    c.advance(c.session)
    assert len(frames) == 1
    assert target not in remote.ids
    assert 'could not be added' in c.status()['selection_error']


def test_song_click_during_saved_confirmation_prevents_the_older_handoff(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    first, latest = state['directions'][1:3]
    c.select(first['id'], state['menu_id'], first['tracks'][2]['video_id'])
    superseded = False

    def choose_during_confirmation():
        nonlocal superseded
        if not superseded:
            superseded = True
            assert c.select(latest['id'], state['menu_id'], latest['tracks'][1]['video_id'])['ok']

    remote.after_add = choose_during_confirmation
    c.advance(c.session)
    assert superseded
    assert len(frames) == 1, 'The superseded request must not navigate during saved confirmation'
    c.advance(c.session)
    assert len(frames) == 2
    assert frames[-1]['url'].endswith(f"v={latest['tracks'][1]['video_id']}&list=PLSESSION")
    assert remote.ids == ['v1', 'v2', latest['tracks'][1]['video_id']]


def test_song_handoff_uses_confirmed_readbacks_without_refreshing_the_old_queue(poc, monkeypatch):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    reads = []
    read = remote.get_playlist

    def count_read(*args, **kwargs):
        reads.append(tuple(remote.ids))
        return read(*args, **kwargs)

    monkeypatch.setattr(remote, 'get_playlist', count_read)
    navigate = c.playback.navigate

    def handoff(*args, **kwargs):
        assert reads == [('v1', 'v2', 'v3'), ('v1', 'v2', 'v3', target)]
        assert not c.ctx.bridge.requests, 'A fresh matching player must not wait for repeated browser observations'
        return navigate(*args, **kwargs)

    monkeypatch.setattr(c.playback, 'navigate', handoff)
    c.ctx.bridge.requests.clear()
    c.ctx.bridge.auto_adopt = False
    c.select(branch['id'], state['menu_id'], target)
    c.advance(c.session)
    assert len(frames) == 2


@pytest.mark.parametrize('change', ['stale-observer', 'stale-heartbeat', 'song-change'])
def test_song_handoff_refreshes_playback_when_confirmation_outlives_its_sample(poc, monkeypatch, change):
    import time

    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    observations = []
    observe = c.listening.observe

    def changed_during_add():
        if change == 'stale-observer':
            c.session.browser_seen_at = time.monotonic() - 9
        elif change == 'stale-heartbeat':
            c.ctx.bridge.now_playing_seen_at = time.monotonic() - 9
        else:
            c.ctx.bridge.now_playing.update(video_id='v3')

    def observe_before_handoff(session):
        if len(frames) == 1 and target in remote.ids:
            observations.append(change)
        return observe(session)

    remote.after_add = changed_during_add
    monkeypatch.setattr(c.listening, 'observe', observe_before_handoff)
    c.select(branch['id'], state['menu_id'], target)
    c.advance(c.session)
    assert observations == [change], 'A changed or stale player must be observed again before handoff'
    assert len(frames) == 2
    if change == 'song-change':
        assert remote.ids == ['v1', 'v2', 'v3', target]
        assert 'v3' in [t['video_id'] for t in c.session.heard]
    else:
        assert remote.ids == ['v1', 'v2', target]


@pytest.mark.parametrize('failure', ['none', 'lost-response', 'lost-read', 'rejected'])
def test_song_starts_before_old_future_cleanup_and_cleanup_survives_failures(poc, monkeypatch, failure):
    import time

    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    remove = remote.remove_playlist_items
    cleanup = []

    def finish_cleanup(*args):
        cleanup.append(list(frames))
        # Model Music starting the selected song while the independent removal
        # is still in flight, before the next observer sample can see it.
        c.ctx.bridge.now_playing = {'video_id': target, 'playlist_ytm_id': 'PLSESSION',
                                   'paused': False, 'position': 30, 'duration': 300}
        c.ctx.bridge.now_playing_seen_at = time.monotonic()
        if failure == 'rejected' and len(cleanup) == 1:
            return {'status': 'STATUS_FAILED'}
        result = remove(*args)
        if failure == 'lost-response' and len(cleanup) == 1:
            raise RuntimeError('Cleanup response lost')
        if failure == 'lost-read' and len(cleanup) == 1:
            remote.fail_read = True
        return result

    monkeypatch.setattr(remote, 'remove_playlist_items', finish_cleanup)
    c.select(branch['id'], state['menu_id'], target)
    if failure == 'lost-read':
        with pytest.raises(RuntimeError, match='Read response lost'):
            c.advance(c.session)
        saved = json.loads(radio_persistence.encode(c.session))
        assert saved['replacement']['play_prefix'] == ['v1', 'v2']
        assert 'play' not in saved['replacement']
        c.session = radio_persistence.restore(saved, remote, c.session.identity, c.session.candidates)
        assert c.session.replacement.play is None
    else:
        c.advance(c.session)
    c.listening.observe(c.session)
    c.advance(c.session)
    assert cleanup and all(len(sent) == 2 for sent in cleanup), 'Playback must precede cleanup transport'
    assert all(sent[-1]['url'].endswith(f'v={target}&list=PLSESSION') for sent in cleanup)
    assert len(frames) == 2, 'Cleanup retries and checkpoint recovery must never replay the click'
    assert remote.ids[:3] == ['v1', 'v2', target]
    assert len(remote.ids) == 4 and c.status()['ahead'] == 1
    assert 'v3' not in [t['video_id'] for t in c.session.heard]
    assert c.session.replacement is None


def test_cleanup_preserves_a_song_actually_heard_during_the_handoff(poc, monkeypatch):
    import time

    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    navigate = c.playback.navigate

    def handoff(*args, **kwargs):
        result = navigate(*args, **kwargs)
        # The old successor was independently observed before Music selected
        # the requested song. It belongs in history and must be retained.
        c.ctx.bridge.now_playing.update(video_id='v3')
        c.ctx.bridge.now_playing_seen_at = time.monotonic()
        c.listening.observe(c.session)
        c.ctx.bridge.now_playing.update(video_id=target)
        return result

    monkeypatch.setattr(c.playback, 'navigate', handoff)
    c.select(branch['id'], state['menu_id'], target)
    c.advance(c.session)
    assert len(frames) == 2
    assert remote.ids[:4] == ['v1', 'v2', 'v3', target]
    assert 'v3' in [t['video_id'] for t in c.session.heard]
    assert c.status()['ahead'] == 1


def test_song_click_wakes_monitor_and_starts_before_candidate_maintenance(poc, monkeypatch):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    waiting, handed_off = threading.Event(), threading.Event()
    failures = []
    wait = c.session.wake.wait
    navigate = c.playback.navigate

    def sleeping(timeout):
        waiting.set()
        return wait(timeout)

    def handoff(*args, **kwargs):
        result = navigate(*args, **kwargs)
        c.suspend()
        handed_off.set()
        return result

    def maintenance(*args):
        raise AssertionError('Candidate maintenance must not delay a requested song')

    def monitor():
        try:
            c._monitor(c.session)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(c.session.wake, 'wait', sleeping)
    monkeypatch.setattr(c.playback, 'navigate', handoff)
    monkeypatch.setattr(c, '_refresh_now', maintenance)
    monkeypatch.setattr(c, '_refresh_candidates', maintenance)
    monkeypatch.setattr(c.listening, 'refresh_metadata', maintenance)
    c.ctx.bridge.auto_adopt = False
    worker = threading.Thread(target=monitor)
    worker.start()
    try:
        assert waiting.wait(2)
        assert c.select(branch['id'], state['menu_id'], target)['ok']
        assert handed_off.wait(1), 'A song click must wake the two-second polling wait immediately'
    finally:
        c.suspend()
        worker.join(timeout=5)
    assert not worker.is_alive() and not failures
    assert len(frames) == 2


@pytest.mark.parametrize('arrives_during', ['observation', 'metadata'])
def test_song_arriving_during_monitor_work_preempts_the_next_maintenance_phase(poc, monkeypatch, arrives_during):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    observe, navigate = c.listening.observe, c.playback.navigate
    chosen = []

    def choose():
        if chosen:
            return
        state = c.status()
        branch = state['directions'][1]
        target = branch['tracks'][2]['video_id']
        chosen.append(target)
        assert c.select(branch['id'], state['menu_id'], target)['ok']

    def observation(session):
        result = observe(session)
        if arrives_during == 'observation':
            choose()
        return result

    def metadata(session):
        if arrives_during == 'metadata':
            choose()
        else:
            pytest.fail('Metadata work must yield to a click received during observation')

    def candidates(session):
        pytest.fail('Candidate work must yield to the click received in this monitor pass')

    def handoff(*args, **kwargs):
        result = navigate(*args, **kwargs)
        c.suspend()
        return result

    monkeypatch.setattr(c.session.wake, 'wait', lambda timeout: None)
    monkeypatch.setattr(c, '_refresh_now', lambda session: None)
    monkeypatch.setattr(c.listening, 'observe', observation)
    monkeypatch.setattr(c.listening, 'refresh_metadata', metadata)
    monkeypatch.setattr(c, '_refresh_candidates', candidates)
    monkeypatch.setattr(c.playback, 'navigate', handoff)
    c._monitor(c.session)
    assert len(frames) == 2
    assert frames[-1]['url'].endswith(f'v={chosen[0]}&list=PLSESSION')


def test_lost_write_response_is_confirmed_without_duplicate_add_or_navigation(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    c.select(branch['id'], state['menu_id'], target)
    remote.lose_response = True
    remote.after_add = lambda: setattr(remote, 'fail_read', True)
    with pytest.raises(RuntimeError, match='Read response lost'):
        c.advance(c.session)
    assert target in remote.ids and len(frames) == 1
    remote.lose_response = False
    remote.after_add = lambda: None
    c.advance(c.session)
    assert len(frames) == 2
    assert remote.adds == [[target]]
    c.advance(c.session)
    assert len(frames) == 2


def test_explicit_song_can_replace_the_future_near_the_end_of_the_current_song(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2', position=299)
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    c.select(branch['id'], state['menu_id'], target)
    c.advance(c.session)
    assert remote.ids == ['v1', 'v2', target]
    assert frames[-1]['url'].endswith(f'v={target}&list=PLSESSION')


def test_lost_extension_connection_reports_playback_failure_without_repeating_it(poc):
    from yt_playlist.core.bridge import BridgeError

    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    c.select(branch['id'], state['menu_id'], branch['tracks'][2]['video_id'])
    attempts = []

    def disconnected(*args, **kwargs):
        attempts.append(args)
        raise BridgeError('Disconnected')

    c.playback.navigate = disconnected
    c.advance(c.session)
    assert 'Reconnect the extension' in c.status()['selection_error']
    c.advance(c.session)
    assert len(attempts) == 1 and len(frames) == 1


def test_explicit_play_switches_from_paused_radio_without_creating_listening_evidence(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    heard = list(c.session.heard)
    play(c, 'v2', paused=True)
    assert c.select(branch['id'], state['menu_id'], target)['ok']
    c.advance(c.session)
    assert remote.ids == ['v1', 'v2', target]
    assert frames[-1]['url'].endswith(f'v={target}&list=PLSESSION')
    assert len(frames) == 2 and c.session.heard == heard


@pytest.mark.parametrize('value', [None, '', ' ', False, 2, [], {}])
def test_song_selection_route_validates_video_before_queue_or_playback_changes(poc, value):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    state = c.status()
    branch = state['directions'][1]
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        result = client.post('/radio/select', json={'proposal_id': branch['id'],
            'menu_id': state['menu_id'], 'video_id': value}).json()
    assert not result['ok']
    assert c.session.requested_direction is None
    assert remote.ids == ['v1', 'v2', 'v3'] and len(frames) == 1


def test_song_selection_route_rejects_stale_and_outside_songs_then_queues_valid_choice(poc):
    c, remote, frames, *_ = poc
    assert c.bootstrap()['state']['can_play_songs'] is True
    assert c.bootstrap()['state']['song_play_version'] == 4
    c.start()
    play(c, 'v2')
    state = c.status()
    assert state['can_play_songs'] is True
    assert state['song_play_version'] == 4
    branch = state['directions'][1]
    target = branch['tracks'][2]['video_id']
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        for menu, video in [('expired', target), (state['menu_id'], 'outside-stream')]:
            result = client.post('/radio/select', json={'proposal_id': branch['id'],
                'menu_id': menu, 'video_id': video}).json()
            assert not result['ok'] and result['stale']
            assert c.session.requested_direction is None
        result = client.post('/radio/select', json={'proposal_id': branch['id'],
            'menu_id': state['menu_id'], 'video_id': target}).json()
        assert result['ok'] and result['updating']
    assert remote.ids == ['v1', 'v2', 'v3'] and len(frames) == 1
    c.advance(c.session)
    assert frames[-1]['url'].endswith(f'v={target}&list=PLSESSION')
