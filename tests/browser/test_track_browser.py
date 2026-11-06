"""Track actions and navigation through the desktop detail page."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_song_actions_browser import song_app  # noqa: F401 - shared fixture


@pytest.mark.parametrize('width', [1280])
def test_track_details_actions_and_navigation(song_app, page, width, tmp_path):
    song_app['store'].tracks.upsert_track('owned-song', 'Owned Song', 'Artist & Friends', 'Album', 180,
                                          thumbnail='/static/nag/identities.png')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('link', name='Owned Song', exact=True).click()
    expect(page.get_by_role('heading', name='Owned Song', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Source mix', exact=True)).to_be_visible()
    page.wait_for_function("document.querySelector('.track-detail-art').naturalWidth > 0")
    art = page.locator('.track-detail-art').bounding_box()
    hero = page.locator('.library-hero.release-hero').bounding_box()
    assert art['width'] >= hero['width'] and art['height'] >= hero['height']
    ledger = page.locator('[aria-labelledby="track-ledger-title"]')
    expect(ledger.get_by_role('heading', name='Listening history')).to_be_visible()
    like = page.get_by_role('button', name='Like', exact=True)
    dislike = page.get_by_role('button', name='Dislike', exact=True)
    expect(like).to_have_text('')
    expect(dislike).to_have_text('')
    expect(page.get_by_role('link', name='Play on YouTube Music')).to_have_text('')
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
