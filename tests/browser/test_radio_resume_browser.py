"""A fresh document can reclaim checkpointed Radio through the real bridge route."""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@pytest.mark.parametrize('path', ['/', '/artists'])
def test_saved_radio_survives_browser_restart_and_reclaims_playback(live_app, store, page, path):
    identity = store.accounts.get_master_account().id
    store.tracks.observe_track_entry('saved-upload', 'Take Me (Mixed)', 'Ajenda', now=1)
    track = store.radio_catalog.details(['saved-upload'])['saved-upload']
    local_id = store.playlists.upsert_playlist(identity, 'PLRECOVERY', 'Radio Session · Saved', 1, '', 1)
    state = dict(version=2, phase='playing', playlist='PLRECOVERY', local_id=local_id,
        identity=identity, title='Radio Session · Saved', episode_id='recovery-episode', started_at=1,
        tracks=[track], heard=[track], position=0, furthest=0, transition=0, choices={},
        fingerprint='controlled-browser-checkpoint', directions=None)
    _, revision = store.radio_checkpoints.load()
    store.radio_checkpoints.save(json.dumps(state), expected=revision)
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(live_app + path)
    player = page.locator('.global-now-playing')
    play = player.get_by_role('button', name='Play', exact=True)
    expect(player).to_be_visible()
    expect(player.locator('.hs-now-title')).to_have_text('Take Me (Mixed)')
    expect(play).to_be_disabled()
    expect(player.locator('.hs-eq')).to_be_hidden()
    assert page.evaluate("playerStatus.toString().includes('radio_session_id')")
    page.reload()  # Discard the entire document's status cache.
    expect(player).to_be_visible()
    expect(play).to_be_disabled()

    with connect(live_app.replace('http://', 'ws://') + '/bridge/ws',
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        page.evaluate('window.playbackStatus.refresh()')
        expect(play).to_be_enabled(timeout=8000)
        expect(player.get_by_role('button', name='Next / skip')).to_be_disabled()
        # No player heartbeat is needed to recover. Merely reconnecting never navigates.
        with pytest.raises(TimeoutError):
            extension.recv(timeout=.2)
        play.click()
        frame = json.loads(extension.recv(timeout=5))
        while frame == {'ping': 1}:
            frame = json.loads(extension.recv(timeout=5))
        assert frame == {'type': 'navigate',
            'url': 'https://music.youtube.com/watch?v=saved-upload&list=PLRECOVERY'}
        expect(play).to_be_enabled()
        expect(player.locator('.hs-eq')).to_be_hidden()
        report = Path('build/test-reports/1153')
        report.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(report / ('recovery-home.png' if path == '/' else 'recovery-artists.png')))
        extension.send(json.dumps({'type': 'play', 'videoId': 'saved-upload', 'title': 'Take Me (Mixed)',
            'artist': 'Ajenda', 'playlist': 'PLRECOVERY', 'paused': False}))
        page.evaluate('window.playbackStatus.refresh()')
        expect(player.get_by_role('button', name='Pause', exact=True)).to_be_enabled(timeout=8000)
        expect(player.locator('.hs-now')).not_to_have_class(re.compile(r'\bpaused\b'))
        expect(player.locator('.hs-eq i').first).to_have_css('animation-play-state', 'running')
        expect(page.locator('#playback-error, #navigation-error, [role="alert"]:visible')).to_have_count(0)


def test_expired_paused_display_resumes_existing_native_player(live_app, page):
    # The bridge expiry itself is covered by test_bridge_route. Control only
    # that display boundary here, retaining the real paused owner and route.
    status = {'connected': True, 'now_playing': None, 'resumable_player': {
        'video_id': 'paused-upload', 'title': 'Paused song', 'artist': 'Artist', 'paused': True}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.set_viewport_size({'width': 1280, 'height': 900})
    with connect(live_app.replace('http://', 'ws://') + '/bridge/ws',
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps({'type': 'play', 'videoId': 'paused-upload',
            'title': 'Paused song', 'artist': 'Artist', 'paused': True}))
        page.goto(live_app + '/artists')
        player = page.locator('.global-now-playing')
        play = player.get_by_role('button', name='Play', exact=True)
        expect(play).to_be_enabled()
        play.click()
        frame = json.loads(extension.recv(timeout=5))
        while frame == {'ping': 1}:
            frame = json.loads(extension.recv(timeout=5))
        assert frame == {'id': frame['id'], 'type': 'playpause', 'action': 'play',
            'videoId': 'paused-upload', 'resumeOnly': True}
        expect(play).to_be_disabled()
        status['now_playing'] = {**status['resumable_player'], 'paused': False}
        extension.send(json.dumps({'id': frame['id'], 'status': 200,
            'body': json.dumps({'ok': True, 'videoId': 'paused-upload', 'paused': False})}))
        expect(player.get_by_role('button', name='Pause', exact=True)).to_be_enabled()
