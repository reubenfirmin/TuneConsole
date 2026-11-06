"""Render exactly one byline and keep hover/keyboard access without underlines."""
import re

from playwright.sync_api import expect

from yt_playlist.rec import account_growth


def test_only_frontier_has_byline_on_initial_load_and_live_progression(live_app, store, page, monkeypatch):
    store.settings.set_setting('onboard_dismissed', '1')
    actual_view = account_growth.view
    frontier = ['vibes']

    def mixed(source, now, **kwargs):
        state = actual_view(source, now, **kwargs)
        # Controlled readiness, deliberate earlier use and optional completion
        # exercise the shipped projection and actual rendered composition.
        for node in state['nodes']:
            node.update(usable=node['id'] != frontier[0], used=node['id'] != frontier[0],
                        state='explored', href='')
        account_growth._connect_paths(state['nodes'])
        return state

    monkeypatch.setattr(account_growth, 'view', mixed)
    page.set_viewport_size({'width': 1700, 'height': 1250})
    page.goto(live_app + '/#unlocks')
    hints = page.locator('.landmark-hint:visible')
    expect(hints).to_have_count(1)
    expect(hints).to_have_text('Preparing')
    expect(hints.locator('xpath=../..')).to_have_attribute('aria-label', re.compile('^Vibes,'))
    labels = page.locator('.landmark-label')
    assert labels.evaluate_all("els => els.every(el => getComputedStyle(el).textDecorationLine === 'none')")
    visited = page.get_by_role('button', name=re.compile('^Mix tapes,'))
    visited.focus()
    visited.press('Enter')
    expect(page.locator('.growth-detail')).to_be_visible()
    page.keyboard.press('Escape')
    page.screenshot(path='/tmp/tuneconsole-862-byline-desktop.png', full_page=True)
    frontier[0] = 'recent'
    page.evaluate("Alpine.$data(document.querySelector('.home-growth')).refresh()")
    expect(hints).to_have_count(1)
    expect(hints.locator('xpath=../..')).to_have_attribute('aria-label', re.compile('^Into recently,'))
