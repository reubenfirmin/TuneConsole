"""Follow Radio album labels whose upload metadata lacks a browse ID."""
import pytest
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui
from tests.conftest import FakeClient
from tests.template_support import web_templates
from tests.web.test_album_navigation import seed_radio_albums

pytestmark = pytest.mark.browser


@pytest.fixture
def home_pages_only(home_pages_only, monkeypatch):
    """Keep the Radio fixture's Home routes and include its real destination."""
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import album

    build = webapp.build_all
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [*build(ctx), album.build(ctx)])


def test_radio_album_memberships_open_the_correct_release(radio_ui, store, page, live_app, monkeypatch):
    seed_radio_albums(store)
    tracks = store.charts.video_tracks_detail(['cards', 'missile'])
    albums = {track['album_browse']: track for track in tracks}
    monkeypatch.setattr(FakeClient, 'get_album', lambda self, browse: {
        'title': albums[browse]['album'], 'artists': [{'name': albums[browse]['artist']}],
        'tracks': []} if browse in albums else {})
    radio_ui.state = {**radio_ui.ready, 'tracks': tracks, 'history_count': 2, 'position': 1,
                      'history': tracks, 'browser_age': 0, 'browser': {'player': {
                          'playlist_ytm_id': 'PLRADIO', 'video_id': 'missile'}}}
    radio_ui.playlist = web_templates().get_template('_partials/radio_playlist.html').render(
        session=radio_ui.state, tracks=tracks)
    for index, track in enumerate(tracks):
        if index:
            page.goto(live_app + '/#radio')
        row = page.locator(f'.radio-history tr[data-vid="{track["video_id"]}"]')
        link = row.get_by_role('link', name=track['album'], exact=True)
        expect(link).to_have_attribute('href', '/album?browse=' + track['album_browse'])
        expect(link).to_be_visible()
        if index == 0:
            page.screenshot(path='build/test-reports/1250-radio-desktop.png', full_page=True)
        link.click()
        expect(page).to_have_url(live_app + '/album?browse=' + track['album_browse'])
        expect(page.get_by_role('heading', name=track['album'], exact=True)).to_be_visible()
