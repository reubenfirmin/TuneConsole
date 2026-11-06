"""An available optional connection lights its tent and spur without a visit."""
import re

from playwright.sync_api import expect

from yt_playlist.rec import account_growth


def test_lastfm_lights_before_any_visit_and_refreshes_with_configuration(live_app, store, page, monkeypatch):
    monkeypatch.setattr(account_growth.lastfm, 'api_key',
                        lambda source: source.settings.get_setting('lastfm_api_key') or None)
    store.settings.set_setting('lastfm_api_key', 'controlled-key')
    page.set_viewport_size({'width': 1700, 'height': 1250})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app + '/#unlocks')
    page.mouse.move(0, 0)
    art = page.locator('[data-landmark="lastfm"]')
    route = page.locator('[data-destination="lastfm"]')
    expect(art).to_have_class(re.compile(r'\bis-connected\b'))
    expect(route).to_be_visible()
    expect(art.locator('.landmark-light')).to_have_css('filter', 'brightness(1.15) saturate(1.15)')
    assert page.locator('.growth-detail').evaluate('el => !el.open')
    state = page.evaluate("Alpine.$data(document.querySelector('.home-growth')).node('lastfm')")
    assert state['connected'] and not state['ready'] and state['state'] == 'preparing'
    page.screenshot(path='/tmp/tuneconsole-860-connected-desktop.png', full_page=True)
    store.settings.set_setting('lastfm_api_key', '')
    page.evaluate("Alpine.$data(document.querySelector('.home-growth')).refresh()")
    expect(art).not_to_have_class(re.compile(r'\bis-connected\b'))
    expect(route).to_be_hidden()
    store.settings.set_setting('lastfm_api_key', 'controlled-key')
    page.reload()
    expect(art).to_have_class(re.compile(r'\bis-connected\b'))
    expect(route).to_be_visible()
    assert not errors
