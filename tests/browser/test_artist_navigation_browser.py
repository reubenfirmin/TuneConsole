"""Follow two identical artist labels without switching their identities."""
import pytest
from playwright.sync_api import expect

from tests.web.test_artist_navigation import seed_artists
from yt_playlist.util.navigation import track_url

pytestmark = pytest.mark.browser


def test_homonymous_album_and_playlist_links_open_their_own_artist(live_app, store, page, monkeypatch):
    _, playlist, (matador, other) = seed_artists(store)
    monkeypatch.setattr('yt_playlist.library.analytics_composition.fetch_artist_info',
        lambda ctx, name, browse: {'name': name, 'browse_id': browse, 'albums': [],
                                  'bio': 'Biography for ' + browse})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(live_app + '/album?browse=B_matador')
    song = {'video_id': 'upload/a+b', 'title': "A & B's ~ (!*) café", 'artist_browse_id': 'UC_matador'}
    assert page.evaluate('song => trackUrl(song)', song) == track_url(song)
    artist = page.locator('.album-artist')
    expect(artist).to_have_attribute('href', f'/artist?artist_id={matador}')
    artist.click()
    expect(page).to_have_url(live_app + f'/artist?artist_id={matador}')
    expect(page.get_by_role('heading', name='Matador', exact=True)).to_be_visible()
    expect(page.locator('#app-page')).to_contain_text('Biography for UC_matador')
    expect(page.get_by_role('link', name='Kingswing', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Other song', exact=True)).to_have_count(0)
    page.screenshot(path='build/test-reports/1245-matador-desktop.png', full_page=True)
    page.goto(live_app + f'/playlist/{playlist}')
    row = page.locator('tr[data-vid="other"]')
    expect(row.get_by_role('link', name='Matador', exact=True)).to_have_attribute('href', f'/artist?artist_id={other}')
    row.get_by_role('link', name='Matador', exact=True).click()
    expect(page).to_have_url(live_app + f'/artist?artist_id={other}')
    expect(page.locator('#app-page')).to_contain_text('Biography for UC_other')
    expect(page.get_by_role('link', name='Other song', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Kingswing', exact=True)).to_have_count(0)
    assert not errors
