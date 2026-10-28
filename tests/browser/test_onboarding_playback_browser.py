"""The starter mix's cue follows a confirmed save into the persistent player."""
import re

from playwright.sync_api import expect
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library.sync import sync_identity


@pytest.fixture
def starter_mix(live_app, store, page):
    iid = store.identities.get_identities()[0].id
    sync_identity(store, iid, FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Ready", "count": 1}],
        tracks={"PL1": [_track("ready", "First playable song", "Artist")]}), now=1)
    store.settings.set_setting("onboard_listening_format", "mixes")
    store.settings.set_setting("onboard_format_pending", "0")
    status = {"connected": True, "now_playing": {
        "video_id": "other", "title": "Already listening", "artist": "Artist",
        "paused": False, "playlist_ytm_id": "OTHER_PLAYLIST"}}
    page.route("**/bridge/status", lambda route: route.fulfill(json=status))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    # Keep the real generation request and redirect, without a loading popup.
    page.add_init_script("window.open = () => null")
    return live_app, status


@pytest.mark.parametrize("width", [390, 1280])
def test_starter_cue_follows_only_its_playback_and_expires(starter_mix, store, page, tmp_path, width):
    base, status = starter_mix
    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
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
    playlist = store.playlists.get_playlist(pid).ytm_playlist_id
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).playlist") == playlist
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))

    # Matching a paused playlist, or losing the bridge, cannot complete the cue.
    status["now_playing"].update(playlist_ytm_id=playlist, paused=True,
                                 video_id="ready", title="First playable song")
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))
    status.update(connected=False)
    status["now_playing"]["paused"] = False
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))

    status["connected"] = True
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).to_have_class(re.compile("is-onboarding-cue"))
    expect(player).to_contain_text("First playable song")
    assert player.evaluate("el => getComputedStyle(el, '::after').animationName") == "onboarding-orbit, onboarding-breathe"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"player-cue-{width}.png"), full_page=True)

    # A reload resumes the remaining eight seconds; fresh polls do not restart it.
    until = page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).until")
    page.clock.install()
    page.clock.run_for(1000)
    page.reload()
    expect(player).to_have_class(re.compile("is-onboarding-cue"))
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).until") == until
    page.clock.fast_forward(8000)
    page.evaluate("window.playbackStatus.refresh()")
    expect(player).not_to_have_class(re.compile("is-onboarding-cue"))
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-playback')") is None

    page.goto(base)
    expect(play).to_be_visible()
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
        expect(page.get_by_role("button", name="Save and play this mix")).to_be_enabled()
    page.evaluate("window.playbackStatus.refresh()")
    expect(page.locator(".global-now-playing")).not_to_have_class(re.compile("is-onboarding-cue"))
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-playback')") is None
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-played')") is None
