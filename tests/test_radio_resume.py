"""Radio can reclaim saved playback and follow native uploads without rewriting its queue."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import (
    completed_radio_schema, completed_radio_store, completed_poc as poc, play,
)
from tests.template_support import web_templates
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_recovery import observed_queue
from yt_playlist.web.routes import radio_session


def test_native_upload_is_heard_and_checkpointed_while_saved_queue_remains_owned(poc):
    c, remote, frames, _, pool = poc
    c.start()
    play(c, 'v2')
    c.ctx.bridge.auto_adopt = False
    c.ctx.bridge.now_playing['video_id'] = 'v30'
    c.ctx.bridge.native_ids = ['v1', 'v2', 'v30']
    c.listening.observe(c.session)
    state = c.status()
    assert state['position'] == 2
    assert [t['video_id'] for t in state['history']] == ['v1', 'v2', 'v30']
    assert state['resume_player']['video_id'] == 'v30'
    assert remote.ids == ['v1', 'v2', 'v3']
    assert c.session.tracks[2]['video_id'] == 'v3'
    assert c.session.heard[-1]['title'] == pool[29]['title']
    c.advance(c.session)
    assert remote.adds == [['v4']]
    assert c.session.observed_video == 'v30'
    c._persist(c.session)
    c.suspend()
    restored = PlaylistRadio(c.ctx)
    restored.join()
    recovered = restored.status()
    assert recovered['resume_player']['video_id'] == 'v30'
    assert [t['video_id'] for t in recovered['history']] == ['v1', 'v2', 'v30']
    assert restored.session.tracks[2]['video_id'] == 'v3'
    assert len(frames) == 1, 'reattachment never navigates automatically'
    restored.ctx.radio = restored
    restored.ctx.templates = web_templates()
    app = FastAPI()
    app.include_router(radio_session.build(restored.ctx))
    with TestClient(app) as client:
        markup = client.get('/radio/playlist').text
    assert 'data-vid="v30"' in markup
    assert 'data-vid="v3"' not in markup
    assert pool[29]['title'] in markup


@pytest.mark.parametrize('native_ids', [['v30', 'v2', 'v1'], ['v1', 'v30'], ['v1', 'v30', 'v30']])
def test_ambiguous_native_queue_does_not_reassign_a_saved_occurrence(poc, native_ids):
    c, *_ = poc
    c.start()
    play(c, 'v1')
    c.ctx.bridge.now_playing['video_id'] = 'v30'
    c.ctx.bridge.native_ids = native_ids
    c.listening.observe(c.session)
    assert c.session.observed_video == 'v1'
    assert c.session.tracks[0]['video_id'] == 'v1'


def test_return_to_original_upload_updates_native_projection_without_losing_listens(poc):
    c, *_ = poc
    c.start()
    play(c, 'v2')
    c.ctx.bridge.auto_adopt = False
    c.ctx.bridge.native_ids = ['v1', 'v2', 'v30']
    play(c, 'v30')
    c.ctx.bridge.native_ids = ['v1', 'v2', 'v3', 'v4']
    play(c, 'v3')
    assert [t['video_id'] for t in observed_queue(c.session.tracks, c.session.heard)] == ['v1', 'v2', 'v3', 'v4']
    assert [t['video_id'] for t in c.status()['history']] == ['v1', 'v2', 'v30', 'v3']
    assert c.status()['resume_player']['video_id'] == 'v3'


def test_upload_change_keeps_an_earlier_listen_at_its_original_position(poc):
    c, *_ = poc
    c.start()
    play(c, 'v3')
    assert [t['video_id'] for t in c.status()['history']] == ['v1', 'v2', 'v3']
    c.ctx.bridge.auto_adopt = False
    c.ctx.bridge.native_ids = ['v1', 'v2', 'v30', 'v4']
    play(c, 'v30')
    assert [t['video_id'] for t in c.status()['history']] == ['v1', 'v2', 'v3', 'v30']


@pytest.mark.parametrize('verified', [False, True])
def test_unknown_upload_requires_verified_exact_metadata_and_keeps_its_own_identity(poc, verified):
    c, remote, *_ = poc
    c.start()
    play(c, 'v2')
    c.ctx.bridge.auto_adopt = False
    c.ctx.bridge.native_ids = ['v1', 'v2', 'unknown-upload']
    original = c.session.tracks[2]
    play(c, 'unknown-upload', title=original['title'], artist=original['artist'], metadata_verified=verified)
    if verified:
        heard = c.status()['history'][-1]
        assert heard['video_id'] == 'unknown-upload'
        assert heard['key'] != original['key']
        assert c.status()['resume_player']['video_id'] == 'unknown-upload'
        assert remote.ids[:3] == ['v1', 'v2', 'v3']
    else:
        assert c.session.observed_video == 'v2'
        assert c.ctx.store.tracks.identity_key_for_video('unknown-upload') is None


def test_direction_change_and_next_still_control_a_substituted_upload(poc, monkeypatch):
    c, remote, *_ = poc
    c.start()
    c.ctx.bridge.auto_adopt = False
    c.ctx.bridge.native_ids = ['v1', 'v30', 'v3']
    play(c, 'v30')
    state = c.status()
    branch = state['directions'][2]
    successor = branch['tracks'][0]['video_id']
    assert c.select(branch['id'], state['menu_id'])['ok']
    c.advance(c.session)
    assert remote.ids == ['v1', 'v2', successor]
    assert remote.removes == [['v3']]
    assert c.session.observed_video == 'v30'
    c.ctx.bridge.native_ids = ['v1', 'v30', successor]
    c.listening.observe(c.session)
    c.advance(c.session)
    assert not c.status()['updating']

    def next_song(video):
        assert video == 'v30', 'native commands retain the exact actual upload'
        c.ctx.bridge.now_playing['video_id'] = successor
        return 200, {'ok': True}

    monkeypatch.setattr(c.playback, 'next', next_song)
    assert c.skip('v30') == {'ok': True}
    assert c.session.observed_video == successor
    assert c.ctx.store.player_events.player_events_since(0, kind='next')[-1]['video_id'] == 'v30'


@pytest.mark.parametrize('restored', [False, True])
def test_explicit_resume_reclaims_checkpoint_without_new_playlist_or_fake_presence(poc, restored):
    c, remote, frames, *_ = poc
    c.start()
    play(c, 'v2')
    c._persist(c.session)
    c.suspend()
    c.ctx.bridge.now_playing = None
    c.ctx.bridge.now_playing_seen_at = None
    if restored:
        c = PlaylistRadio(c.ctx)
        c.ctx.radio = c
    target = c.state()['resume_player']
    app = FastAPI()
    app.include_router(radio_session.build(c.ctx))
    before = len(frames)
    with TestClient(app) as client:
        assert client.get('/radio/status').json()['resume_player'] == target
        assert len(frames) == before, 'polling must not restart playback'
        data = {'playlist_id': target['playlist_ytm_id'], 'video_id': target['video_id'],
                'episode_id': target['radio_session_id']}
        assert client.post('/radio/resume', json=data).json() == {'ok': True}
        assert frames[-1] == {'type': 'navigate',
            'url': 'https://music.youtube.com/watch?v=v2&list=PLSESSION'}
        assert c.ctx.bridge.now_playing is None
        assert remote.ids == ['v1', 'v2', 'v3']
        assert remote.adds == remote.removes == []
        assert client.post('/radio/resume', json={**data, 'video_id': 'v3'}).json() == {'ok': False}
        assert client.post('/radio/resume', json={**data, 'episode_id': 'older'}).json() == {'ok': False}
        assert len(frames) == before + 1


@pytest.mark.parametrize('restored', [False, True])
def test_stop_and_superseding_intent_fence_radio_resume(poc, restored):
    c, _, frames, *_ = poc
    c.start()
    play(c, 'v1')
    target = c.state()['resume_player']
    old = c.ctx.bridge.begin_playback_intent()
    c.ctx.bridge.begin_playback_intent()
    assert c.resume_playback('PLSESSION', 'v1', target['radio_session_id'], intent=old) == {'ok': False}
    if restored:
        c._persist(c.session)
        c.suspend()
        c.ctx.bridge.now_playing = None
        c.ctx.bridge.now_playing_seen_at = None
        c = PlaylistRadio(c.ctx)
    c.stop()
    assert c.state()['resume_player'] is None
    assert c.resume_playback('PLSESSION', 'v1', target['radio_session_id'],
                             intent=c.ctx.bridge.begin_playback_intent()) == {'ok': False}
    assert len(frames) == 1
