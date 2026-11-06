"""Ready paths remain connected without illuminating output ahead of its gate."""
from playwright.sync_api import expect

from yt_playlist.rec import account_growth


def test_ready_trails_light_before_use_and_later_output_cannot_skip_a_gate(live_app, page, monkeypatch):
    actual_view = account_growth.view
    blocked = ['foundation']

    def prepared_ahead(source, now, **kwargs):
        state = actual_view(source, now, **kwargs)
        # Controlled usable output at every later stop challenges the real
        # serial projection; none has successful-use evidence.
        for node in state['nodes']:
            node.update(usable=node['id'] != blocked[0] and not node['optional'],
                        used=False, state='ready', href='')
        account_growth._connect_paths(state['nodes'])
        return state

    monkeypatch.setattr(account_growth, 'view', prepared_ahead)
    page.set_viewport_size({'width': 1700, 'height': 1250})
    page.goto(live_app + '/#unlocks')
    routes = page.locator('.festival-routes')
    expect(routes.locator('.growth-route:visible')).to_have_count(2)
    expect(routes.locator('[data-destination=first]')).to_have_class('growth-route is-ready')
    expect(routes.locator('[data-destination=first]')).to_be_visible()
    expect(routes.locator('.is-preparing:visible')).to_have_attribute('data-destination', 'foundation')
    expect(routes.locator('[data-destination=listening]')).to_be_hidden()
    original = routes.locator('path').evaluate_all('els => els.map(el => el.getAttribute("d"))')

    blocked[0] = 'tv'
    page.evaluate("Alpine.$data(document.querySelector('.home-growth')).refresh()")
    expect(routes.locator('.is-ready:visible')).to_have_count(6)
    expect(routes.locator('.is-preparing:visible')).to_have_attribute('data-destination', 'tv')
    expect(routes.locator('[data-destination=vibes]')).to_be_hidden()
    expect(routes.locator('[data-destination=recent]')).to_be_hidden()
    expect(page.locator('.growth-legend')).to_have_text('Open paths')
    assert routes.locator('path').evaluate_all('els => els.map(el => el.getAttribute("d"))') == original
    page.screenshot(path='/tmp/tuneconsole-861-ready-paths-desktop.png', full_page=True)
