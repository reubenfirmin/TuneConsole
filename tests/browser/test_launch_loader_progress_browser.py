"""Fresh On air artwork progresses without replacing its moving diagram."""
import pytest
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui


RADIO = 'Alpine.$data(document.querySelector(".radio-home"))'


@pytest.mark.parametrize('width', [1440, 390])
def test_new_seed_is_visible_first_and_staged_art_keeps_every_orbital_node(page, radio_ui, width):
    api = radio_ui
    page.clock.install()
    page.set_viewport_size({'width': width, 'height': 1000})
    api.state = api.ready
    page.evaluate(f'void {RADIO}.refresh()')
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    page.locator('.radio-menu > summary').click()
    page.get_by_role('button', name='New radio', exact=True).click()
    expect(page.locator('.radio-seeds')).to_be_visible()
    page.evaluate('''window.firstLaunchFrames = [];
      function captureLaunch() {
        const root = document.querySelector('.radio-launch');
        if (root && getComputedStyle(root).display !== 'none' && firstLaunchFrames.length < 10)
          firstLaunchFrames.push([...root.querySelectorAll('.gen-node img')].map(img => img.src));
        requestAnimationFrame(captureLaunch);
      }
      requestAnimationFrame(captureLaunch);''')
    page.locator('.radio-seed-start').nth(8).click()
    root = page.locator('.radio-launch')
    expect(root).to_be_visible()
    expect(root.locator('.gen-node img')).to_have_count(1)
    expect(root.locator('.gen-node img')).to_have_attribute('src', api.tracks[8]['thumbnail'])
    page.wait_for_function('window.firstLaunchFrames.length > 0')
    assert page.evaluate('window.firstLaunchFrames[0]') == [api.tracks[8]['thumbnail']]
    page.clock.pause_at(page.evaluate('Date.now()/1000') + .05)
    page.evaluate('''window.orbitalNodes = [...document.querySelectorAll('.radio-launch .gen-node')];
      window.orbitalTransforms = orbitalNodes.map(node => node.style.transform);
      window.seedImage = orbitalNodes.find(node => node.querySelector('img'))?.firstChild;
      window.stems = document.querySelector('.radio-launch canvas').toDataURL();''')
    api.state = {**api.ready, 'playlist_id': 'NEW_RADIO', 'local_id': 2, 'starting': True,
                 'tracks': api.tracks[8:10]}
    api.playlist = api.playlist.replace('data-vid="radio0"', 'data-vid="radio8"')
    api.starts.pop().fulfill(json={'ok': True, **api.state})
    expect(root.locator('.gen-node img')).to_have_count(2)
    api.state = {**api.state, 'tracks': api.tracks[8:12]}
    page.evaluate(f'void {RADIO}.refresh()')
    expect(root.locator('.gen-node img')).to_have_count(4)
    assert page.evaluate('''orbitalNodes.every((node,i) =>
      node === document.querySelectorAll('.radio-launch .gen-node')[i] &&
      node.style.transform === orbitalTransforms[i])''')
    assert page.evaluate('seedImage.isConnected')
    assert page.evaluate('document.querySelector(".radio-launch canvas").toDataURL() === stems')
    page.screenshot(path=f'/tmp/tuneconsole-422-progress-{width}.png')
    page.clock.resume()
    # An old status response cannot repaint the next launch with obsolete art.
    pending = []
    page.route('**/radio/status', lambda route: pending.append(route))
    page.evaluate(f'void {RADIO}.refresh()')
    expect(root).to_be_visible()
    page.evaluate(f'void {RADIO}.newRadio()')
    expect(page.locator('.radio-seeds')).to_be_visible()
    page.locator('.radio-seed-start').nth(12).click()
    expect(root.locator('.gen-node img')).to_have_count(1)
    expect(root.locator('.gen-node img')).to_have_attribute('src', api.tracks[12]['thumbnail'])
    for route in pending:
        route.fulfill(json=api.ready)
    expect(root.locator('.gen-node img')).to_have_count(1)
    expect(root.locator('.gen-node img')).to_have_attribute('src', api.tracks[12]['thumbnail'])


def test_missing_broken_and_late_old_art_leave_the_same_nodes_and_fresh_images(page, radio_ui):
    api = radio_ui
    page.locator('.radio-seed-start').first.click()
    root = page.locator('.radio-launch')
    expect(root).to_be_visible()
    page.evaluate('''window.artLoader = Alpine.$data(document.querySelector('.radio-launch .gen-loader'));
      window.artNodes = [...document.querySelectorAll('.radio-launch .gen-node')];
      window.oldImageError = artNodes.find(node => node.querySelector('img')).firstChild.onerror;''')
    # No URL creates a blank moving slot, not stale session artwork.
    page.evaluate('artLoader.update({thumbs: [], active: true})')
    expect(root.locator('.gen-node img')).to_have_count(0)
    page.route('**/broken-loader-art.svg', lambda route: route.fulfill(status=404))
    page.evaluate('artLoader.update({thumbs: ["/broken-loader-art.svg"], active: true})')
    expect(root.locator('.gen-node img')).to_have_count(0)
    page.evaluate('artLoader.update({thumbs: ["/static/favicon.svg"], active: true}); oldImageError()')
    expect(root.locator('.gen-node img')).to_have_count(1)
    expect(root.locator('.gen-node img')).to_have_attribute('src', '/static/favicon.svg')
    assert page.evaluate('artNodes.every((node,i) => node === document.querySelectorAll(".radio-launch .gen-node")[i])')
