"""Live-behavior tests for the playlist detail page (/playlist/{id}).

Own fixture (a playlist with a couple of tracks; the shared live_app seeds discover data
and is off-limits). Characterization first: lock the CURRENT Alpine behavior, then keep it
green after each Alpine->htmx conversion (rename, year/genre cells, enrich, remove, reorder,
alternates). Asserts on user-visible outcomes, not the transport.
"""
import socket
import threading
import time
import re
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN
from tests.conftest import FakeClient

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_playlist_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("Main", "cred", None, True)
    pid = s.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    t0 = s.tracks.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)
    t1 = s.tracks.upsert_track("v1", "Song B", "Artist Y", "Alb", 200, 1)
    s.playlists.set_playlist_tracks(pid, [t0, t1])
    s.tracks.set_track_genre(t0, "Rock")            # one track starts with a genre, one blank
    client = FakeClient(
        tracks={"PL1": [{"videoId": "v0", "setVideoId": "sv0"},
                        {"videoId": "v1", "setVideoId": "sv1"}]},
        catalog={"valt": {"videoId": "valt", "setVideoId": "svalt", "title": "Song A (Live)",
                           "artists": [{"name": "Artist X"}], "duration_seconds": 210}},
        search_results=[  # alternate-version search (source is excluded by find_alternates)
            {"videoId": "v0", "title": "Song A", "artists": [{"name": "Artist X"}], "duration_seconds": 200},
            {"videoId": "valt", "title": "Song A (Live)", "artists": [{"name": "Artist X"}], "duration_seconds": 210}])
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "pid": pid, "store": s, "client": client}
    server.should_exit = True
    thread.join(timeout=5)


def test_rename_playlist_updates_heading(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("heading", name="Mix").click()        # click-to-edit
    inp = page.locator("input.title-input")
    inp.fill("Renamed Mix")
    inp.press("Enter")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
    # survives a reload (persisted, not just a DOM poke)
    page.goto(f"{base}/playlist/{pid}")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()


@pytest.mark.parametrize('width,font_scale', [
    (320, 1), (320, 1.5), (320, 2), (390, 1), (390, 1.5), (720, 1.5), (1440, 1)])
def test_generated_heading_keeps_the_full_title_when_editing_and_after_rename(
        live_playlist_app, page, width, font_scale, tmp_path):
    base, pid, store = (live_playlist_app[key] for key in ('base', 'pid', 'store'))
    original = 'Metal & "more" - More in your wheelhouse - October 26 2026 #12'
    store.playlists.set_playlist_title(pid, original, 1)
    store.playlists.mark_generated_playlist('PL1')
    page.set_viewport_size({'width': width, 'height': 950})
    page.goto(f'{base}/playlist/{pid}')
    if font_scale != 1:
        page.add_style_tag(content=f'html {{ font-size: {font_scale * 100}% !important; }}')
    page.evaluate('document.fonts.ready')
    heading = page.get_by_role('heading', name='Metal & "more"', exact=True)
    expect(heading).to_be_visible()
    expect(page.locator('.pl-type-badge')).to_have_text('More in your wheelhouse')
    expect(page.locator('.pl-version')).to_have_text('Version 12')
    expect(page.locator('time.pl-generated-date')).to_have_text('October 26, 2026')
    expect(page.locator('time.pl-generated-date')).to_have_attribute('datetime', '2026-10-26')
    expect(page.locator('.pl-generated-meta time')).to_have_count(1)
    assert page.locator('.pl-generated-meta').bounding_box()['y'] < heading.bounding_box()['y']
    page.screenshot(path=str(tmp_path / 'generated-playlist.png'), full_page=True)
    layout = page.evaluate('''() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
      overflow:[...document.querySelectorAll('body *')].map(el => ({tag:el.tagName,
        cls:typeof el.className === 'string' ? el.className : '',text:el.textContent.slice(0,80),
        right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width}))
        .filter(el => el.right > innerWidth + 1 && el.width)})''')
    (tmp_path / 'generated-playlist-layout.json').write_text(json.dumps(layout, indent=2))
    assert layout['scrollWidth'] <= width, layout
    for selector in ('.topbar .brand', '.sidebar-privacy'):
        bounds = page.locator(selector).bounding_box()
        assert bounds and 0 <= bounds['x'] < bounds['x'] + bounds['width'] <= width
    assert page.locator('.release-hero').evaluate('''hero => {
      const bounds = hero.getBoundingClientRect();
      const tools = hero.querySelector('.head-tools').getBoundingClientRect();
      const contained = r => r.left >= bounds.left && r.right <= bounds.right + 1
          && r.top >= bounds.top && r.bottom <= bounds.bottom + 1;
      const controlsFit = [...hero.querySelectorAll('.head-tools,.head-icon')]
        .filter(el => el.getClientRects().length).every(el => contained(el.getBoundingClientRect()));
      return controlsFit && [...hero.querySelectorAll('h1,.pl-type-badge,.pl-version,.pl-generated-date')].every(el => {
        const r = el.getBoundingClientRect();
        const overlapsTools = Math.min(r.right, tools.right) > Math.max(r.left, tools.left)
          && Math.min(r.bottom, tools.bottom) > Math.max(r.top, tools.top);
        return contained(r) && !overlapsTools;
      });
    }''')
    for name in ('app.css', 'library-tables.css', 'library-controls.css', 'library-entities.css'):
        css = page.request.get(f'{base}/static/{name}')
        assert css.status == 200
        assert css.body() == (Path(__file__).resolve().parents[2]
                              / 'src/yt_playlist/web/static' / name).read_bytes()
    row = page.locator('#playlist-track-body tr').first
    expect(row.locator('.drag-handle')).to_be_visible()
    expect(row.locator('.like-btn')).to_be_visible()
    menu_button = row.get_by_role('button', name='More actions for Song A', exact=True)
    bounds = menu_button.bounding_box()
    assert bounds and 0 <= bounds['x'] < bounds['x'] + bounds['width'] <= width
    menu_button.click()
    menu = page.get_by_role('menu')
    expect(menu).to_be_visible()
    expect(menu.get_by_role('menuitem', name='Edit Metadata', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    expect(menu).not_to_be_visible()
    heading.focus()
    heading.press('Enter')
    editor = page.locator('input.title-input')
    expect(editor).to_have_value(original)
    editor.fill('Canceled draft')
    editor.press('Escape')
    expect(heading).to_be_visible()
    assert store.playlists.get_playlist(pid).title == original
    heading.click()
    expect(editor).to_have_value(original)
    renamed = 'Ambient - Comfort listening - October 27 2026 #2'
    editor.fill(renamed)
    editor.press('Enter')
    expect(page.get_by_role('heading', name='Ambient', exact=True)).to_be_visible()
    expect(page.locator('.pl-type-badge')).to_have_text('Comfort listening')
    expect(page.locator('.pl-version')).to_have_text('Version 2')
    expect(page.locator('time.pl-generated-date')).to_have_text('October 27, 2026')
    assert store.playlists.get_playlist(pid).title == renamed
    assert live_playlist_app['client'].edited == [('PL1', {'title': renamed})]
    page.reload()
    expect(page.get_by_role('heading', name='Ambient', exact=True)).to_be_visible()
    if font_scale != 1:
        page.add_style_tag(content=f'html {{ font-size: {font_scale * 100}% !important; }}')
    page.evaluate('document.fonts.ready')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('video', [False, True])
def test_playlist_header_groups_play_radio_and_counts(live_playlist_app, page, width, video):
    base, pid, store = (live_playlist_app[key] for key in ('base', 'pid', 'store'))
    store.identities.upsert_identity('Other', 'other-cred', 'other-brand', False)
    if video:
        playlist = store.playlists.get_playlist(pid)
        store.playlist_recipes.set_recipe(playlist.scope_ref, {'media': 'video'})
    page.set_viewport_size({'width': width, 'height': 950})
    page.goto(f'{base}/playlist/{pid}')
    actions = page.locator('.release-hero .pl-actions')
    play = actions.get_by_role('link' if video else 'button',
                               name='Play on TV' if video else 'Play on YouTube Music', exact=True)
    expect(play).to_be_visible()
    expect(play.locator('svg')).to_have_count(1)
    expect(play).to_have_text('')
    if video:
        expect(play).to_have_attribute('href', f'/?tv_playlist={pid}#tv')
    expect(page.locator('.release-hero')).not_to_contain_text('Account:')
    counts = actions.locator('.pl-stats')
    expect(counts).to_contain_text('2 tracks')
    expect(counts).to_contain_text('0 total plays')
    radio = actions.get_by_role('link', name='Start radio from this playlist', exact=True)
    expect(radio).to_have_attribute('href', f'/?radio_playlist={pid}#radio')
    assert counts.bounding_box()['x'] >= radio.bounding_box()['x'] + radio.bounding_box()['width']
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('width', [390, 1440])
def test_playlist_header_follows_only_its_confirmed_queue_without_relaunching(live_playlist_app, page, width):
    base, pid = (live_playlist_app[key] for key in ('base', 'pid'))
    status = {'connected': True, 'now_playing': {
        'video_id': 'v0', 'title': 'Song A', 'artist': 'Artist X',
        'playlist_ytm_id': 'PL1', 'paused': False}}
    pending, launches, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.route('**/now-playing/toggle?*', lambda route: pending.append(route))

    def launch(route):
        launches.append(route.request.post_data_json)
        route.fulfill(json={'ok': True})

    page.route('**/play', launch)
    page.set_viewport_size({'width': width, 'height': 950})
    page.goto(f'{base}/playlist/{pid}')
    header = page.locator('.pl-actions .playlist-play')
    player = page.locator('.global-now-playing')

    def assert_state(playing):
        label = 'Pause' if playing else 'Play'
        expect(header).to_have_attribute('aria-label', label + ' on YouTube Music')
        expect(header).to_have_attribute('title', label + ' on YouTube Music')
        expect(header.locator('path')).to_have_attribute('d',
            'M6 4h4v16H6zm8 0h4v16h-4z' if playing else 'M7 3.5v17l14-8.5Z')
        expect(player.get_by_role('button', name=label, exact=True)).to_be_visible()

    assert_state(True)
    for index, action in enumerate(['pause', 'play']):
        header.click()
        expect(header).to_be_disabled()
        expect(header).to_have_attribute('aria-busy', 'true')
        assert_state(action == 'pause')  # Wait for observation; no optimistic icon flip.
        header.evaluate('el => Alpine.$data(el).play()')
        assert len(pending) == index + 1
        assert parse_qs(urlsplit(pending[-1].request.url).query) == {
            'action': [action], 'videoId': ['v0'], 'confirm': ['true']}
        status['now_playing']['paused'] = action == 'pause'
        pending[-1].fulfill(json={'ok': True, 'paused': action == 'pause'})
        expect(header).to_be_enabled()
        assert_state(action == 'play')
    assert launches == []
    page.reload()
    assert_state(True)

    # A shared song does not identify the displayed queue. Each must launch normally.
    for queue in [
        {'playlist_ytm_id': 'OTHER'},
        {'playlist_ytm_id': 'ALBUM', 'source': {'kind': 'album'}},
        {'playlist_ytm_id': 'PL1', 'source': {'kind': 'radio'}},
        {},
    ]:
        status['now_playing'] = {'video_id': 'v0', 'title': 'Song A', 'artist': 'Artist X',
                                 'paused': False, **queue}
        page.evaluate('window.playbackStatus.refresh()')
        expect(header).to_have_attribute('aria-label', 'Play on YouTube Music')
        expect(player.get_by_role('button', name='Pause', exact=True)).to_be_visible()
        with page.expect_response('**/play'):
            header.click()
        expect(header).to_be_enabled()
    assert launches == [{'url': 'https://music.youtube.com/playlist?list=PL1'}] * 4
    assert len(pending) == 2

    status.update(connected=False, now_playing={
        'video_id': 'v0', 'playlist_ytm_id': 'PL1', 'paused': False})
    page.evaluate('window.playbackStatus.refresh()')
    expect(header).to_have_attribute('aria-label', 'Play on YouTube Music')

    # A paused background player's last confirmed queue resumes through its owned player.
    status.update(connected=True, now_playing=None, resumable_player={
        'video_id': 'v0', 'playlist_ytm_id': 'PL1', 'paused': True})
    page.evaluate('window.playbackStatus.refresh()')
    header.click()
    expect(header).to_be_disabled()
    assert parse_qs(urlsplit(pending[-1].request.url).query) == {
        'action': ['play'], 'videoId': ['v0'], 'confirm': ['true'], 'resume': ['true']}
    status.update(now_playing={
        'video_id': 'v0', 'title': 'Song A', 'artist': 'Artist X',
        'playlist_ytm_id': 'PL1', 'paused': False}, resumable_player=None)
    pending[-1].fulfill(json={'ok': True, 'paused': False})
    expect(header).to_be_enabled()
    assert_state(True)
    assert len(pending) == 3 and len(launches) == 4
    expect(page).to_have_url(f'{base}/playlist/{pid}')
    assert len(page.context.pages) == 1
    assert errors == []


@pytest.mark.parametrize('failure', ['rejected', 'network', 'invalid_json', 'unconfirmed'])
def test_playlist_header_retains_observed_state_when_control_is_not_confirmed(live_playlist_app, page, failure):
    base, pid = (live_playlist_app[key] for key in ('base', 'pid'))
    status = {'connected': True, 'now_playing': {
        'video_id': 'v0', 'title': 'Song A', 'artist': 'Artist X',
        'playlist_ytm_id': 'PL1', 'paused': False}}
    pending, launches = [], []
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.route('**/now-playing/toggle?*', lambda route: pending.append(route))
    page.on('request', lambda request: launches.append(request.url) if request.url.endswith('/play') else None)
    page.goto(f'{base}/playlist/{pid}')
    header = page.get_by_role('button', name='Pause on YouTube Music', exact=True)
    header.click()
    expect(header).to_be_disabled()
    if failure == 'network':
        pending[0].abort()
    elif failure == 'invalid_json':
        pending[0].fulfill(content_type='text/plain', body='unavailable')
    else:
        pending[0].fulfill(json={'ok': failure == 'unconfirmed', 'paused': False})
    expect(page.locator('#playback-error')).to_contain_text('Playback was not confirmed')
    expect(header).to_be_enabled()
    expect(page.locator('.global-now-playing').get_by_role('button', name='Pause', exact=True)).to_be_visible()
    assert len(pending) == 1 and launches == []
    header.click()
    expect(header).to_be_disabled()
    status['now_playing']['paused'] = True
    pending[-1].fulfill(json={'ok': True, 'paused': True})
    expect(page.get_by_role('button', name='Play on YouTube Music', exact=True)).to_be_enabled()
    expect(page.locator('#playback-error')).to_have_count(0)
    assert len(pending) == 2 and launches == []


def test_set_year_from_song_menu(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.get_by_role('button', name='More actions for Song B').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    dialog.get_by_label('Year', exact=True).fill('1999')
    dialog.get_by_role('button', name='Save changes').click()
    expect(dialog).not_to_be_visible()
    expect(row.get_by_text("1999")).to_be_visible()        # cell shows the new year
    page.goto(f"{base}/playlist/{pid}")                    # persisted
    expect(page.get_by_role("row").filter(has_text="Song B").get_by_text("1999")).to_be_visible()


def test_conflict_provider_opens_lookup_without_changing_choice(live_playlist_app, page):
    base, pid, store = (live_playlist_app[key] for key in ("base", "pid", "store"))
    tid = store.charts.playlist_tracks_detail(pid)[0]["track_id"]
    store.tracks.set_track_enrichment(tid, None, "1952")
    store.enrichment.upsert_conflict(tid, "year", [{"provider": "musicbrainz", "value": "1952"},
                                         {"provider": "discogs", "value": "2001"}])
    page.context.route("https://www.discogs.com/**", lambda route: route.fulfill(body="Provider results"))
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("button", name="Resolve enrichment disagreements").click()
    modal = page.locator("#conflicts-modal")
    original, alternative = modal.get_by_role("radio", name="1952"), modal.get_by_role("radio", name="2001")
    expect(original).to_be_checked()

    with page.expect_popup() as opened:
        modal.get_by_role("link", name="discogs").click()
    popup = opened.value
    popup.wait_for_load_state()
    assert "q=Artist+X+Song+A" in popup.url
    popup.close()
    expect(original).to_be_checked()
    expect(modal.get_by_role("heading", name="Resolve enrichment disagreements")).to_be_visible()

    alternative.check()
    modal.get_by_role("button", name="Save choices").click()
    expect(modal.get_by_text("Nothing left to resolve.", exact=False)).to_be_visible()
    assert store.charts.playlist_tracks_detail(pid)[0]["year"] == "2001"


def test_song_menu_metadata_accepts_custom_genre(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.get_by_role('button', name='More actions for Song B').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Song B')
    dialog.get_by_label('Genre', exact=True).fill('Jazz Fusion')
    dialog.get_by_role('button', name='Save changes').click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("heading", name="Mix")).to_be_visible()
    row = page.get_by_role("row").filter(has_text="Song B")
    expect(row.get_by_text("Jazz Fusion", exact=True)).to_be_visible()


def test_genre_badge_is_display_only(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    row.get_by_text("Rock", exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    row.get_by_role('button', name='More actions for Song A').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Rock')


@pytest.mark.parametrize("initial_status", ["INDIFFERENT", "LIKE"])
def test_now_playing_heart_immediately_updates_matching_playlist_row(live_playlist_app, page, initial_status):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True,
        "now_playing": {"title": "Song A", "artist": "Artist X", "thumbnail": "",
                        "likeStatus": initial_status, "video_id": "v0", "paused": False},
        "sensor_health": {"healthy": True, "ytm_tabs": 1, "responding_tabs": 1,
                          "reinjected_tabs": 0, "error": ""},
        "radio": False, "radio_waiting": False, "radio_dual": False,
        "radio_fallback_reason": None, "radio_upcoming": [],
    }))
    page.route("**/now-playing/rate", lambda route: route.fulfill(json={"ok": True}))
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    expect(row.locator(".like-btn")).to_have_attribute("aria-pressed", str(initial_status == "LIKE").lower())
    desired_status = "INDIFFERENT" if initial_status == "LIKE" else "LIKE"
    with page.expect_request(lambda request: request.url.endswith("/now-playing/rate")) as request:
        page.locator(".global-now-playing .hs-heart").click()
    assert request.value.post_data_json == {"action": "like", "videoId": "v0", "likeStatus": desired_status}
    expect(row.locator(".like-btn")).to_have_attribute("aria-pressed", str(desired_status == "LIKE").lower())


@pytest.mark.parametrize("initial_status", ["", "INDIFFERENT", "LIKE"])
@pytest.mark.parametrize("control", ["table", "sidebar"])
def test_playing_track_heart_uses_backend_and_survives_stale_sensor(live_playlist_app, page, initial_status, control):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    snapshot = {"type": "now-heartbeat", "title": "Song A", "artist": "Artist X",
                "videoId": "v0", "likeStatus": initial_status, "paused": False}
    desired_status = "INDIFFERENT" if initial_status == "LIKE" else "LIKE"
    pressed = "true" if desired_status == "LIKE" else "false"
    with connect(base.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps(snapshot))
        page.goto(f"{base}/playlist/{pid}")
        widget = page.locator(".global-now-playing")
        heart = page.get_by_role("row").filter(has_text="Song A").locator(".like-btn")
        expect(widget).to_be_visible()
        expect(heart).to_have_attribute("aria-pressed", "false" if pressed == "true" else "true")

        target = heart if control == "table" else widget.locator(".hs-heart")
        with page.expect_response(lambda r: r.url.endswith("/now-playing/rate"), timeout=5000) as response:
            target.click()
            native = json.loads(extension.recv(timeout=5))
            assert native == {"id": native["id"], "type": "rate", "action": "like",
                              "videoId": "v0", "likeStatus": desired_status, "brandId": "", "skip": False}
            extension.send(json.dumps({"id": native["id"], "status": 200, "body": json.dumps({"ok": True})}))
        assert response.value.json()["ok"] is True
        expect(heart).to_have_attribute("aria-pressed", pressed)
        assert live_playlist_app["client"].rated == [("v0", desired_status)]
        # YouTube's player bar is not refreshed by the API write. Even its old LIKE readout
        # after an unlike must not overwrite the confirmed backend state or taste feedback.
        extension.send(json.dumps({**snapshot, "type": "play", "brandId": ""}))
        page.evaluate("""() => {
          window.ratingPolled = false;
          window.addEventListener('track-like-changed', () => { window.ratingPolled = true; }, {once: true});
        }""")
        with page.expect_response(lambda r: r.url.endswith("/bridge/status"), timeout=8000) as polled:
            pass
        assert polled.value.json()["now_playing"]["likeStatus"] == desired_status
        page.wait_for_function("window.ratingPolled")
        expect(heart).to_have_attribute("aria-pressed", pressed)
        if pressed == "true":
            expect(widget.locator(".hs-heart")).to_have_class(re.compile(r"\bon\b"))
        else:
            expect(widget.locator(".hs-heart")).not_to_have_class(re.compile(r"\bon\b"))

        # Recreate Alpine state to prove this is durable, rather than its optimistic timeout.
        page.reload()
        expect(widget).to_be_visible()
        expect(heart).to_have_attribute("aria-pressed", pressed)
        assert live_playlist_app["client"].rated == [("v0", desired_status)]


def test_other_track_table_heart_keeps_using_liked_music(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    with connect(base.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps({"type": "now-heartbeat", "title": "Song A", "artist": "Artist X",
                                   "videoId": "v0", "likeStatus": "INDIFFERENT", "paused": False}))
        page.goto(f"{base}/playlist/{pid}")
        expect(page.locator(".global-now-playing")).to_be_visible()
        heart = page.get_by_role("row").filter(has_text="Song B").locator(".like-btn")
        with page.expect_response(lambda r: r.url.endswith("/track/like")):
            heart.click()
        expect(heart).to_have_attribute("aria-pressed", "true")
        assert live_playlist_app["client"].rated == [("v1", "LIKE")]
        with page.expect_response(lambda r: r.url.endswith("/bridge/status"), timeout=8000):
            pass
        expect(heart).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".global-now-playing .hs-heart")).not_to_have_class(re.compile(r"\bon\b"))


@pytest.mark.parametrize("failure", ["rejected", "network"])
def test_playing_track_table_heart_reverts_on_failed_rating(live_playlist_app, page, failure):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    if failure == "rejected":
        page.route("**/now-playing/rate", lambda route: route.fulfill(json={"ok": False}))
    else:
        page.route("**/now-playing/rate", lambda route: route.abort())
    with connect(base.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps({"type": "now-heartbeat", "title": "Song A", "artist": "Artist X",
                                   "videoId": "v0", "likeStatus": "INDIFFERENT", "paused": False}))
        page.goto(f"{base}/playlist/{pid}")
        expect(page.locator(".global-now-playing")).to_be_visible()
        heart = page.get_by_role("row").filter(has_text="Song A").locator(".like-btn")
        heart.click()
        expect(heart).to_have_attribute("aria-pressed", "false")
        expect(page.locator(".global-now-playing .hs-heart")).not_to_have_class(re.compile(r"\bon\b"))
        assert live_playlist_app["client"].rated == []


def test_remove_track_drops_row(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.get_by_title("More actions").click()                        # ⋯ menu
    page.get_by_role("menuitem", name="Remove from playlist").click()  # opens confirm modal
    page.get_by_role("button", name="Remove", exact=True).click()   # confirm
    expect(page.get_by_role("row").filter(has_text="Song B")).to_have_count(0)
    expect(page.get_by_role("link", name="Song A", exact=True)).to_be_visible()


@pytest.mark.parametrize("indexing_lag", [False, True])
def test_find_and_add_alternate_version(live_playlist_app, page, indexing_lag):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    client = live_playlist_app["client"]
    if indexing_lag:
        client._catalog["valt"].pop("setVideoId")
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    row.get_by_title("More actions").click()
    page.get_by_role("menuitem", name="Find alternate versions").click()
    expect(page.get_by_text("Song A (Live)")).to_be_visible()        # htmx-rendered search results
    page.locator('#alt-results input[name="track"]').first.check()
    page.get_by_role("button", name="Add to playlist").click()
    # HX-Refresh reload -> the chosen alternate is now a row in the table
    expect(page.get_by_role("link", name="Song A (Live)", exact=True)).to_be_visible()
    retry = page.get_by_role("button", name="Retry placement")
    if indexing_lag:
        expect(page.get_by_role("status").filter(has_text="placement after the original song")).to_be_visible()
        assert [t["videoId"] for t in client._tracks["PL1"]] == ["v0", "v1", "valt"]
        client._catalog["valt"]["setVideoId"] = "svalt"
        retry.click()
    expect(retry).to_have_count(0)
    assert [t["videoId"] for t in client._tracks["PL1"]] == ["v0", "valt", "v1"]
    assert [t["video_id"] for t in live_playlist_app["store"].charts.playlist_tracks_detail(pid)] == ["v0", "valt", "v1"]
    assert client.added == [("PL1", ["valt"])]


def test_enrich_updates_cells_live(live_playlist_app, page, monkeypatch):
    # Enrichment is one waterfall button now (the header's "Enrich" icon runs every enabled
    # provider in order); isolate MusicBrainz and stub its lookup.
    import yt_playlist.providers.musicbrainz as mb
    from tests.conftest import only_provider
    monkeypatch.setattr(mb, "enrich_full",
                        lambda title, artist, *, observations=None: ("Electronic", "1998", None, [], []) if title == "Song B"
                        else (None, None, None, [], []))
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    only_provider(live_playlist_app["store"], "musicbrainz")
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("button", name="Enrich", exact=True).click()
    row = page.get_by_role("row").filter(has_text="Song B")
    expect(row.get_by_text("Electronic")).to_be_visible(timeout=5000)   # live SSE cell update
    expect(row.get_by_text("1998")).to_be_visible()


def test_generated_feedback_updates_after_background_enrichment(live_playlist_app, page):
    store, pid = live_playlist_app['store'], live_playlist_app['pid']
    tracks = store.charts.playlist_tracks_detail(pid)
    for track in tracks:
        store.tracks.set_track_genre(track['track_id'], '')
    store.playlists.set_playlist_group('PL1', 'Generated')
    store.playlists.mark_generated_playlist('PL1')
    page.goto(f"{live_playlist_app['base']}/playlist/{pid}")
    page.get_by_role('button', name='Detailed feedback', exact=True).click()
    expect(page.get_by_text('These tracks need genre and release-year information.')).to_be_visible()
    page.get_by_role('button', name='More of this vibe', exact=True).click()
    for track in tracks:
        store.tracks.set_track_genre(track['track_id'], 'Techno')
        store.tracks.set_track_year(track['track_id'], '1995')
    row = page.get_by_role('row').filter(has_text='Song B')
    expect(row.get_by_text('Techno', exact=True)).to_be_visible(timeout=6000)
    expect(page.get_by_role('button', name='More techno', exact=True)).to_be_visible(timeout=6000)
    expect(page.get_by_role('button', name='More 1990s', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Detailed feedback', exact=True)).to_have_attribute('aria-expanded', 'true')
    expect(page.get_by_role('button', name='More of this vibe', exact=True)).to_have_attribute('aria-pressed', 'true')
    with page.expect_response(lambda r: r.url.endswith('/recs/mood') and r.request.method == 'POST') as mood:
        page.get_by_role('button', name='More techno', exact=True).click()
    assert mood.value.ok
    expect(page.get_by_role('button', name='More techno', exact=True)).to_have_attribute('aria-pressed', 'true')
    assert set(store.rec_signals.recent_mood_events()[0][2]) == {t['identity_key'] for t in tracks}


def test_generated_feedback_updates_after_enrich_and_metadata_edit(live_playlist_app, page, monkeypatch):
    import yt_playlist.providers.musicbrainz as mb
    from tests.conftest import only_provider

    store, pid = live_playlist_app['store'], live_playlist_app['pid']
    # Keep the untagged track: a manually chosen blank genre would correctly override the provider.
    track = next(t for t in store.charts.playlist_tracks_detail(pid) if t['video_id'] == 'v1')
    store.playlists.set_playlist_tracks(pid, [track['track_id']])
    store.playlists.set_playlist_group('PL1', 'Generated')
    store.playlists.mark_generated_playlist('PL1')
    only_provider(store, 'musicbrainz')
    monkeypatch.setattr(mb, 'enrich_full', lambda *args, **kwargs: ('Techno', '1995', None, [], []))
    page.goto(f"{live_playlist_app['base']}/playlist/{pid}")
    page.get_by_role('button', name='Detailed feedback', exact=True).click()
    expect(page.get_by_text('These tracks need genre and release-year information.')).to_be_visible()
    page.get_by_role('button', name='Enrich', exact=True).click()
    expect(page.get_by_role('button', name='More techno', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='More 1990s', exact=True)).to_be_visible()
    response = page.wait_for_event('response', predicate=lambda r: r.url.endswith(f'/playlist/{pid}/cells'))
    assert response.json()['pending'] == 0
    # Once metadata is complete the polling loop stops. Editing the shared song dialog must
    # still update the facet controls without reloading or collapsing Detailed feedback.
    row = page.get_by_role('row').filter(has_text='Song B')
    row.get_by_role('button', name='More actions for Song B').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Techno')
    dialog.get_by_label('Genre', exact=True).fill('Ambient')
    dialog.get_by_label('Year', exact=True).fill('2004')
    dialog.get_by_role('button', name='Save changes').click()
    expect(row.get_by_text('Ambient', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='More ambient', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='More 2000s', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Detailed feedback', exact=True)).to_have_attribute('aria-expanded', 'true')
    with page.expect_response(lambda r: r.url.endswith('/recs/mood') and r.request.method == 'POST') as mood:
        page.get_by_role('button', name='More ambient', exact=True).click()
    assert mood.value.ok
    expect(page.get_by_role('button', name='More ambient', exact=True)).to_have_attribute('aria-pressed', 'true')
    key = track['identity_key']
    assert store.rec_signals.recent_mood_events()[0][2] == [key]
