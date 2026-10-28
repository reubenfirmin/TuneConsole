"""The two launch surfaces share moving artwork and one console status."""
import pytest
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui


def moving_graph(page, root):
    expect(root.locator('.gen-node')).to_have_count(8)
    expect(root.locator('h2')).to_have_count(1)
    assert 'mono' in root.locator('h2').evaluate('el => getComputedStyle(el).fontFamily').lower()
    first = root.locator('.gen-node').first
    start = first.evaluate('el => el.style.transform')
    page.wait_for_function("({selector, start}) => document.querySelector(selector).style.transform !== start",
                           arg={'selector': '.gen-stage .gen-node', 'start': start})
    radii = root.evaluate("""el => {
      const hub = el.querySelector('.gen-hub').getBoundingClientRect();
      return [...el.querySelectorAll('.gen-node')].map(node => {
        const r = node.getBoundingClientRect();
        return Math.hypot(r.x+r.width/2-hub.x-hub.width/2, r.y+r.height/2-hub.y-hub.height/2);
      });
    }""")
    assert max(radii) - min(radii) > 12
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@pytest.mark.parametrize('width', [1440, 390])
def test_popup_rotates_one_status_and_uses_varied_moving_orbits(page, live_app, width):
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_init_script("localStorage.setItem('tc_gen_thumbs', JSON.stringify(Array.from({length:8}, (_,i) => '/static/favicon.svg?'+i)))")
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app + '/home/generating')
    root = page.get_by_role('status', name='Preparing playlist')
    moving_graph(page, root)
    expect(root.locator('[data-loader-status]')).to_have_text('Building on YouTube Music', timeout=5000)
    copy = root.locator('.gen-copy').bounding_box()
    assert all(node['y'] + node['height'] < copy['y']
               for node in root.locator('.gen-node').evaluate_all('els => els.map(el => el.getBoundingClientRect().toJSON())'))
    page.screenshot(path=f'/tmp/tuneconsole-422-popup-{width}.png')
    assert not errors


def test_on_air_loader_stops_off_panel_and_cleans_up_on_removal(page, radio_ui):
    page.locator('.radio-seed').first.click()
    root = page.locator('.radio-launch')
    moving_graph(page, root)
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    expect(root.locator('.gen-loader')).to_have_class('gen-loader is-loader-paused')
    start = root.locator('.gen-node').first.evaluate('el => el.style.transform')
    page.wait_for_timeout(150)
    assert root.locator('.gen-node').first.evaluate('el => el.style.transform') == start
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(root.locator('.gen-loader')).to_have_class('gen-loader')
    page.evaluate("Alpine.$data(document.querySelector('.radio-launch .gen-loader')).destroy()")
    expect(root.locator('.gen-loader')).to_have_class('gen-loader is-loader-paused')


@pytest.mark.parametrize('width', [1440, 390])
def test_radio_loader_disappears_after_connection_and_stays_hidden_while_tuning(page, radio_ui, width):
    from tests.template_support import web_templates

    api = radio_ui
    page.set_viewport_size({'width': width, 'height': 1000})
    api.playlist = web_templates().get_template('_partials/radio_playlist.html').render(
        session=api.ready, tracks=api.tracks)
    page.locator('.radio-seed-start').first.click()
    loader = page.locator('.radio-launch')
    expect(loader).to_be_visible()
    expect(loader.locator('.gen-hub')).to_be_visible()
    page.evaluate('''window.finishedLoader = document.querySelector('.radio-launch .gen-loader');
      window.finishedLoaderNodes = [...finishedLoader.querySelectorAll('.gen-node')];''')
    api.state = api.ready
    api.starts.pop().fulfill(json={'ok': True, **api.ready})
    expect(page.locator('.radio-session')).to_be_visible(timeout=10000)
    page.screenshot(path=f'/tmp/tuneconsole-411-loader-connected-{width}.png')
    expect(loader).to_be_hidden()
    expect(loader.locator('.gen-hub')).to_be_hidden()
    expect(loader.locator('.gen-node').first).to_be_hidden()
    expect(loader.locator('.gen-loader')).to_have_count(0)
    assert page.evaluate('!finishedLoader.isConnected && finishedLoaderNodes.every(node => !node.isConnected)')

    page.get_by_role('button', name='Tune streams', exact=True).click()
    dialog = page.get_by_role('dialog', name='Tune streams')
    dialog.get_by_role('slider', name='Chill to Full On').fill('0')
    page.screenshot(path=f'/tmp/tuneconsole-411-loader-chill-{width}.png')
    expect(loader).to_be_hidden()
    expect(loader.locator('[data-loader-status]')).to_be_hidden()
    page.keyboard.press('Escape')
    expect(page.locator('.radio-session')).to_be_visible()
    expect(loader).to_be_hidden()

    # Returning to an existing session takes the reconnect path, without a
    # launch song. Tuning must never remount the completed loader either.
    page.reload()
    expect(page.locator('.radio-session')).to_be_visible(timeout=10000)
    expect(loader.locator('.gen-loader')).to_have_count(0)
    page.get_by_role('button', name='Tune streams', exact=True).click()
    dialog.get_by_role('slider', name='Chill to Full On').fill('0')
    api.state = {**api.ready, 'tuning': {'comfort': .5, 'theme': .5, 'energy': 0}}
    page.route('**/radio/tune', lambda route: route.fulfill(json={'ok': True, **api.state}))
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator('.radio-session')).to_be_visible()
    expect(loader).to_be_hidden()
    expect(loader.locator('.gen-loader')).to_have_count(0)
