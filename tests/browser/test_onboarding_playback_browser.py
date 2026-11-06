"""The starter mix's cue follows a confirmed save into the persistent player."""
import re

from playwright.sync_api import expect
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library.sync import sync_identity


@pytest.fixture
def starter_mix(live_app, store, page):
    iid = store.accounts.get_accounts()[0].id
    sync_identity(store, iid, FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Ready", "count": 1}],
        tracks={"PL1": [_track("ready", "First playable song", "Artist")]}), now=1)
    store.settings.set_setting("onboard_listening_format", "mixes")
    status = {"connected": True, "now_playing": {
        "video_id": "other", "title": "Already listening", "artist": "Artist",
        "paused": False, "playlist_ytm_id": "OTHER_PLAYLIST"}}
    page.route("**/bridge/status", lambda route: route.fulfill(json=status))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    # Keep the real generation request and redirect, without a loading popup.
    page.add_init_script("window.open = () => null")
    return live_app, status


@pytest.mark.parametrize("width", [1280])
def test_starter_cue_follows_only_its_playback_and_expires(starter_mix, store, page, tmp_path, width):
    base, status = starter_mix
    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    byline = page.locator('.festival-intro-sub')
    expect(byline).to_have_text('Your first set is ready')
    # Any live playback still explores First sounds, but only the offered set
    # can change this first-set-specific byline.
    store.tracks.observe_track_entry('unrelated', 'Other music', 'Other artist', now=2)
    store.history.record_play_event(store.accounts.get_accounts()[0].id,
        store.tracks.identity_key_for_video('unrelated'), 'unrelated', 2, playlist_ytm_id='UNRELATED')
    page.reload()
    expect(byline).to_have_text('Your first set is ready')
    assert page.request.get(base + '/home/growth').json()['nodes'][0]['state'] == 'explored'
    play = page.get_by_role("button", name="Save and play this mix")
    expect(play).to_be_visible()
    assert play.evaluate("el => getComputedStyle(el, '::after').animationName") == "onboarding-orbit, onboarding-breathe"
    player = page.locator(".global-now-playing")
    expect(player).to_contain_text("Already listening")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".starter-mix").evaluate("""el => Promise.all(el.getAnimations({subtree: true})
        .filter(animation => animation.effect.getComputedTiming().iterations !== Infinity)
        .map(animation => animation.finished))""")
    page.screenshot(path=str(tmp_path / f"starter-cue-{width}.png"), full_page=True)

    play.click()
    page.wait_for_url(re.compile(r"/playlist/\d+$"))
    pid = int(page.url.rsplit("/", 1)[1])
    playlist = store.playlists.get_playlist(pid).provider_playlist_id
    header = page.locator('.pl-actions .playlist-play')
    expect(header).to_have_attribute('aria-label', 'Play on YouTube Music')
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).playlist") == playlist
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))

    # Matching a paused playlist, or losing the bridge, cannot complete the cue.
    status["now_playing"].update(playlist_ytm_id=playlist, paused=True,
                                 video_id="ready", title="First playable song")
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))
    expect(header).to_have_attribute('aria-label', 'Play on YouTube Music')
    status.update(connected=False)
    status["now_playing"]["paused"] = False
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))
    expect(header).to_have_attribute('aria-label', 'Play on YouTube Music')

    status["connected"] = True
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).to_have_class(re.compile("is-onboarding-cue"))
    expect(player).to_contain_text("First playable song")
    expect(header).to_have_attribute('aria-label', 'Pause on YouTube Music')
    assert player.evaluate("el => getComputedStyle(el, '::after').animationName") == "onboarding-orbit, onboarding-breathe"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"player-cue-{width}.png"), full_page=True)

    # A reload resumes the remaining eight seconds; fresh polls do not restart it.
    until = page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).until")
    page.clock.install()
    page.clock.run_for(1000)
    page.reload()
    expect(player).to_have_class(re.compile("is-onboarding-cue"))
    expect(header).to_have_attribute('aria-label', 'Pause on YouTube Music')
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).until") == until
    page.clock.fast_forward(8000)
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-playback')") is None

    page.goto(base)
    expect(play).to_be_visible()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    page.reload()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    assert play.evaluate("el => getComputedStyle(el, '::after').animationName") == "none"
    assert errors == []


@pytest.mark.parametrize("failure", ["friendly", "http"])
def test_unsaved_starter_does_not_advance_cue(starter_mix, page, failure):
    base, status = starter_mix
    # A real application-level rejection uses the normal friendly result template.
    # An HTTP failure covers a request that cannot be swapped at all.
    if failure == "friendly":
        page.route("**/home/generate", lambda route: route.continue_(
            post_data="name=First+mix&tracks=%5B%5D"))
    else:
        page.route("**/home/generate", lambda route: route.fulfill(status=503, body="Unavailable"))
    page.goto(base)
    page.get_by_role("button", name="Save and play this mix").click()
    if failure == "friendly":
        expect(page.locator(".gen-done")).to_contain_text("Couldn't save")
    else:
        expect(page.get_by_role("button", name="Save and play this mix")).to_be_disabled()
        expect(page.locator('[data-generation-error]')).to_contain_text('couldn’t confirm playback')
    page.evaluate("window.playbackStatus.refresh()")
    expect(page.locator(".global-now-playing")).not_to_have_class(re.compile("is-onboarding-cue"))
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-playback')") is None
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-played')") is None


@pytest.mark.parametrize('width', [1280])
def test_long_mix_discovered_at_play_time_recovers_into_artist_choices(live_app, store, page,
                                                                      monkeypatch, tmp_path, width):
    from yt_playlist.library import playlist_generated

    iid = store.accounts.get_accounts()[0].id
    sync_identity(store, iid, FakeClient(
        playlists=[{'playlistId': 'PL1', 'title': 'Imported music', 'count': 2}],
        tracks={'PL1': [_track('known-mix', 'Known long DJ mix', 'Artist', dur=9000),
                        _track('unknown-mix', 'Length not imported yet', 'Artist', dur=None)]}), now=1)
    store.settings.set_setting('last_sync_at', '1')
    # YouTube supplies this length only when the user starts the proposed set.
    lookups = []

    def song_duration(client, video):
        lookups.append(video)
        return 9000

    monkeypatch.setattr(playlist_generated, 'fetch_song_duration', song_duration)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True, 'now_playing': None}))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.add_init_script('window.open = () => null')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    expect(page.locator('.starter-tracks .gen-title')).to_have_text('Length not imported yet')
    page.get_by_role('button', name='Save and play this mix').click()
    result = page.locator('.gen-done')
    expect(result).to_contain_text('20-minute maximum track length set in Taste Model')
    expect(result).not_to_contain_text('unexpected response')
    expect(result.get_by_role('link', name='Change track-length limit')).to_have_attribute(
        'href', '/taste#recommendations/features')
    assert lookups == ['unknown-mix']
    assert store.tracks.duration_for_video('unknown-mix') == 9000
    assert store.actions.get_actions() == []
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-playback')") is None
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'track-length-error-{width}.png'), full_page=True)

    result.get_by_role('link', name='Find another set').click()
    expect(page.get_by_role('heading', name='Pick 3 artists you love.')).to_be_visible()
    assert errors == []
