"""Live-behavior tests for the Save/Unsave album action on the artist page (/artist).

This is the albums-tab refactor target: the only fetch() in the albums/artist area is
the saveAlbum toggle, which lives on artist.html (the /albums page itself is pure client
sort). Saving must keep the collection table, the saved column, and the discography table
in sync, so the action does a full reload, converted to htmx HX-Refresh (parity).

Own fixture (shared live_app seeds discover data and is off-limits). Characterization
first: lock the CURRENT Alpine toggle, then keep it green after the htmx conversion.
"""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

pytestmark = pytest.mark.browser


class AlbumClient(FakeClient):
    """FakeClient that can resolve + rate an album (what save/unsave-album needs)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rated = []

    def get_album(self, browse_id):
        return {"audioPlaylistId": "PLAUDIO", "title": "The Album", "type": "Album",
                "artists": [{"name": "Artist X"}], "year": "2001", "thumbnails": [],
                "tracks": [{"videoId": "v1", "title": "SongA", "duration": "3:20",
                            "artists": [{"name": "Artist X"}]},
                           {"videoId": "v2", "title": "SongB", "duration": "3:00"}]}

    def rate_playlist(self, playlist_id, rating):
        self.rated.append((playlist_id, rating))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_artist_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("Main", "cred", None, True)
    other = s.identities.upsert_identity("Other", "cred", "other", False)
    pid = s.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    # a track with an album browse id -> the collection table renders a Save button for that album
    t = s.tracks.upsert_track("v1", "SongA", "Artist X", "The Album", 200, 1, album_browse_id="MPREb_x")
    s.playlists.set_playlist_tracks(pid, [t])
    client = AlbumClient()
    clients = {iid: client, other: AlbumClient()}
    app = create_app(s, lambda: clients, now_fn=lambda: 1.0, background_work=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "store": s, "iid": iid, "other": other, "clients": clients}
    server.should_exit = True
    thread.join(timeout=5)


def test_save_album_flips_to_unsave_after_reload(live_artist_app, page):
    page.goto(f"{live_artist_app['base']}/artist?name=Artist%20X")
    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_visible()
    save.click()
    expect(page.get_by_role("button", name="Unsave", exact=True)).to_be_visible()   # now saved


@pytest.mark.parametrize('width', [1440, 390])
def test_album_status_track_links_and_save_from_artist(live_artist_app, page, width):
    base = live_artist_app['base']
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(f'{base}/album?browse=MPREb_x')
    save = page.get_by_role('button', name='Save album to library', exact=True)
    expect(save).to_be_visible()
    expect(save).to_be_in_viewport(ratio=1)
    expect(page.get_by_text('1 of 2 tracks in your library. The album is not saved.', exact=True)).to_be_visible()
    rows = page.locator('.track-table tbody tr.srow')
    expect(rows).to_have_count(2)
    expect(rows.locator('a.ptitle')).to_have_count(2)
    expect(rows.locator('.ui-arrow--external')).to_have_count(0)
    expect(rows.locator('a.pl-play')).to_have_count(2)
    expect(rows.locator('a.alink')).to_have_count(2)
    expect(rows.locator('a.alink').last).to_have_attribute('href', '/artist?name=Artist%20X')
    assert page.locator('.track-table').evaluate('''table =>
        Math.max(...[...table.querySelectorAll('thead th')].map(th => th.getBoundingClientRect().bottom)) <=
        table.querySelector('tbody tr').getBoundingClientRect().top + 1''')
    page.screenshot(path=f'/tmp/tuneconsole-album-preview-{width}.png')
    rows.locator('a.alink').first.click()
    expect(page.get_by_role('heading', name='Artist X', exact=True)).to_be_visible()
    albums = page.locator('table:has(colgroup.artist-album-columns) tbody > tr')
    expect(albums).to_have_count(1)
    expect(albums.get_by_role('link', name='The Album', exact=True)).to_have_attribute('href', '/album?browse=MPREb_x')
    expect(page.locator('a.ptitle[href*="watch?v="]')).to_have_count(0)
    page.get_by_role('button', name='Save', exact=True).click()
    expect(page.get_by_role('button', name='Unsave', exact=True)).to_be_visible()
    page.goto(f'{base}/album?browse=MPREb_x')
    expect(page.get_by_role('button', name='Save album to library', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name="Remove album from Main's library", exact=True)).to_be_visible()
    expect(page.locator('.track-table tbody tr.srow')).to_have_count(2)
    expect(page.locator('.track-table a.ptitle')).to_have_count(2)
    expect(page.locator('.track-table a.alink')).to_have_count(2)


def test_unsave_album_flips_to_save_after_reload(live_artist_app, page):
    # start from a saved album so the button reads "Unsave"
    live_artist_app["store"].collection.add_saved_album({
        "browse": "MPREb_x", "title": "The Album", "artist": "Artist X",
        "year": "2001", "thumbnail": ""}, identity_id=live_artist_app["iid"])
    page.goto(f"{live_artist_app['base']}/artist?name=Artist%20X")
    unsave = page.get_by_role("button", name="Unsave", exact=True)
    expect(unsave).to_be_visible()
    unsave.click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_visible()      # now unsaved


@pytest.mark.parametrize('width', [1440, 390])
def test_existing_album_page_uses_shared_player_and_library_icons(live_artist_app, page, width):
    status = {'connected': True, 'now_playing': {
        'video_id': 'v1', 'title': 'SongA', 'artist': 'Artist X', 'paused': False,
        'playlist_ytm_id': 'PLAUDIO', 'position': 30, 'duration': 200}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    toggles, starts, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def toggle(route):
        toggles.append(route.request.url)
        status['now_playing']['paused'] = not status['now_playing']['paused']
        route.fulfill(json={'ok': True})

    def start(route):
        starts.append(route.request.post_data)
        route.fulfill(status=204)

    page.route('**/now-playing/toggle', toggle)
    page.route('**/album/play', start)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_artist_app['base'] + '/album?browse=MPREb_x')
    expect(page.locator('.album-release-hero')).to_be_visible()
    expect(page.locator('.album-page-head')).to_have_count(0)
    current = page.locator('.track-table tr.now-playing')
    expect(current).to_have_attribute('data-vid', 'v1')
    pause = page.get_by_role('button', name='Pause The Album', exact=True)
    expect(pause).to_be_visible()
    pause.click()
    play = page.get_by_role('button', name='Play The Album', exact=True)
    expect(play).to_be_visible()
    play.click()
    expect(pause).to_be_visible()
    assert len(toggles) == 2 and not starts
    status['now_playing'].update(video_id='v2', title='SongB', paused=True, position=5, duration=180)
    page.evaluate('window.playbackStatus.refresh()')
    expect(current).to_have_attribute('data-vid', 'v2')
    progress = page.locator('.global-now-playing').get_by_role('progressbar', name='Track progress')
    expect(progress).to_have_attribute('value', '5')
    expect(progress).to_have_attribute('max', '180')
    # Hearing this album's song in another queue must not pause that other queue.
    status['now_playing'].update(playlist_ytm_id='another-playlist', paused=False)
    page.evaluate('window.playbackStatus.refresh()')
    play.click()
    assert len(starts) == 1 and len(toggles) == 2
    expect(page.locator('.pl-actions a[href*="music.youtube.com"]')).to_have_count(0)
    save = page.get_by_role('button', name='Save album to library', exact=True)
    expect(save.locator('svg')).to_have_attribute('fill', 'none')
    save.click()
    saved = page.get_by_role('button', name="Remove album from Main's library", exact=True)
    expect(saved).to_have_attribute('aria-pressed', 'true')
    expect(saved).to_have_class('head-icon album-library on')
    expect(saved.locator('svg')).to_have_attribute('fill', 'currentColor')
    assert saved.evaluate('el => getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
    page.screenshot(path=f'/tmp/tuneconsole-392-album-{width}.png', full_page=True)
    assert not errors


def test_non_main_album_owner_is_removed_from_collection(live_artist_app, page):
    app = live_artist_app
    app['store'].collection.add_saved_album({
        'browse': 'MPREb_x', 'title': 'The Album', 'artist': 'Artist X'}, identity_id=app['other'])
    page.goto(f"{app['base']}/albums")
    expect(page.get_by_role('button', name='Unsave', exact=True)).to_be_visible()
    expect(page.get_by_text('Other', exact=True)).to_be_visible()
    page.get_by_role('button', name='Unsave', exact=True).click()
    expect(page.get_by_text('No saved albums found in your YouTube library.')).to_be_visible()
    assert app['clients'][app['iid']].rated == []
    assert app['clients'][app['other']].rated == [('PLAUDIO', 'INDIFFERENT')]


def test_multiple_owners_offer_explicit_account_actions(live_artist_app, page):
    app = live_artist_app
    for owner in (app['iid'], app['other']):
        app['store'].collection.add_saved_album({
            'browse': 'MPREb_x', 'title': 'The Album', 'artist': 'Artist X'}, identity_id=owner)
    page.goto(f"{app['base']}/artist?name=Artist%20X")
    expect(page.get_by_role('button', name='Unsave from Main', exact=True)).to_be_visible()
    page.get_by_role('button', name='Unsave from Other', exact=True).click()
    expect(page.get_by_role('button', name='Unsave', exact=True)).to_be_visible()
    assert app['store'].collection.get_saved_albums(identity_id=app['other']) == []
    assert app['store'].collection.get_saved_albums(identity_id=app['iid'])
    assert app['clients'][app['iid']].rated == []


def test_unavailable_album_owner_shows_error_and_retains_membership(live_artist_app, page):
    app = live_artist_app
    app['store'].collection.add_saved_album({
        'browse': 'MPREb_x', 'title': 'The Album', 'artist': 'Artist X'}, identity_id=app['other'])
    app['clients'].pop(app['other'])
    page.goto(f"{app['base']}/albums")
    page.get_by_role('button', name='Unsave', exact=True).click()
    expect(page.get_by_text('The required account is not connected')).to_be_visible()
    assert app['store'].collection.get_saved_albums(identity_id=app['other'])
    assert app['clients'][app['iid']].rated == []
