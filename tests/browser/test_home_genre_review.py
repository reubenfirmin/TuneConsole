"""Independent checks for the limits of Home genre draft preservation."""
import hashlib
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.test_home_browser import genre_home_template, prepared_genre_app, store


@pytest.mark.parametrize('replacement', ['reset', 'navigation'])
def test_explicit_replacement_discards_unsent_genre_draft(
        prepared_genre_app, store, page, replacement, output_path):
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    with page.expect_response(lambda response: '/static/home-tuning.js?' in response.url) as loaded:
        page.goto(f'{prepared_genre_app}/')
    source = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static/home-tuning.js'
    assert hashlib.sha256(loaded.value.body()).digest() == hashlib.sha256(source.read_bytes()).digest()

    launcher = page.get_by_role('button', name='Tune these mixes', exact=True)
    launcher.click()
    panel = page.locator('#fingerprint')
    panel.get_by_role('checkbox', name='jazz', exact=True).check()
    panel.get_by_role('checkbox', name='techno', exact=True).check()
    panel.locator('#home-make-mixes').click()
    expect(panel.locator('dialog')).not_to_be_visible()
    launcher.click()
    panel.get_by_role('checkbox', name='jazz', exact=True).uncheck()
    panel.get_by_role('checkbox', name='techno', exact=True).uncheck()
    panel.get_by_role('checkbox', name='classical', exact=True).check()
    panel.get_by_role('searchbox', name='Find a genre for these mixes').fill('classical')

    if replacement == 'reset':
        panel.get_by_role('button', name='Reset mix adjustments', exact=True).click()
        expect(panel.locator('dialog')).not_to_be_visible()
    else:
        page.evaluate("navigation.navigate('/playlists').finished")
        expect(panel).to_have_count(0)
        page.evaluate("navigation.navigate('/').finished")

    if not panel.locator('dialog').is_visible():
        launcher.click()
    expect(panel.locator('dialog')).to_be_visible()
    expect(panel.get_by_role('searchbox', name='Find a genre for these mixes')).to_have_value('')
    selected = panel.locator('#home-genre-options input:checked').evaluate_all('els => els.map(el => el.value)')
    assert set(selected) == (set() if replacement == 'reset' else {'jazz', 'techno'})
    expect(panel.get_by_role('checkbox', name='classical', exact=True)).not_to_be_checked()
    page.screenshot(path=Path(output_path) / 'after-explicit-replacement.png')
