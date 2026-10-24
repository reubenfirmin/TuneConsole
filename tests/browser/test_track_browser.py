"""Track actions and navigation work on desktop and narrow screens."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_song_actions_browser import song_app  # noqa: F401 - shared fixture
from tests.test_song_actions import SONG
from yt_playlist.web.track_links import track_url


@pytest.mark.parametrize('width', [1280, 390])
def test_track_details_actions_and_navigation(song_app, page, width, tmp_path):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('link', name='Owned Song', exact=True).click()
    expect(page.get_by_role('heading', name='Owned Song', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Source mix', exact=True)).to_be_visible()
    like = page.get_by_role('button', name='Like', exact=True)
    dislike = page.get_by_role('button', name='Dislike', exact=True)
    like.click()
    expect(like).to_have_attribute('aria-pressed', 'true')
    dislike.click()
    expect(dislike).to_have_attribute('aria-pressed', 'true')
    expect(like).to_have_attribute('aria-pressed', 'false')
    assert song_app['main'].rated[-2:] == [('owned-song', 'LIKE'), ('owned-song', 'DISLIKE')]
    page.get_by_role('button', name='Create playlist with song', exact=True).click()
    dialog = page.get_by_role('dialog', name='Create playlist', exact=True)
    expect(dialog.get_by_label('Playlist name')).to_be_focused()
    dialog.get_by_label('Playlist name').fill('Track favorites')
    dialog.get_by_role('button', name='Create playlist', exact=True).click()
    expect(dialog.get_by_text('Playlist created.', exact=True)).to_be_visible()
    dialog.get_by_role('button', name='Done', exact=True).click()
    expect(page.get_by_role('link', name='Track favorites', exact=True)).to_be_visible()
    page.get_by_role('button', name='Edit metadata', exact=True).click()
    edit = page.get_by_role('dialog', name='Edit Metadata', exact=True)
    edit.get_by_label('Title', exact=True).fill('Corrected song')
    edit.get_by_role('button', name='Save changes', exact=True).click()
    expect(page.get_by_role('heading', name='Corrected song', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=tmp_path / f'track-{width}.png', full_page=True, animations='disabled')
    assert not errors


def test_discovery_album_save_and_rating_error(song_app, page, monkeypatch):
    page.goto(song_app['base'] + track_url(SONG))
    expect(page.get_by_role('heading', name=SONG['title'], exact=True)).to_be_visible()
    page.route('**/now-playing/rate', lambda route: route.fulfill(
        status=200, json={'ok': False, 'error': 'Rating unavailable'}))
    page.get_by_role('button', name='Like', exact=True).click()
    expect(page.get_by_role('alert').filter(has_text='Rating unavailable')).to_be_visible()
    expect(page.get_by_role('button', name='Like', exact=True)).to_have_attribute('aria-pressed', 'false')
    song_app['main']._albums['MPRE_new']['audioPlaylistId'] = 'ALBUM_PLAYLIST'
    monkeypatch.setattr(song_app['main'], 'rate_playlist', lambda *args: None, raising=False)
    page.get_by_role('button', name='Save album to collection', exact=True).click()
    expect(page.get_by_text('Saved to collection', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Save album to collection', exact=True)).to_have_count(0)
