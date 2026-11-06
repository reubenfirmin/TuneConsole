"""Quiet recovery and ordinary navigation retain Home's accessible destination."""
import pytest
from playwright.sync_api import expect

from tests.home_fixture_support import browser_store as store
from tests.browser_fixtures import live_app
from tests.browser.test_home_vibe_browser import published_vibe_controls
from tests.browser.quiet_action_support import action_evidence, assert_quiet_action


@pytest.mark.parametrize('fragment,selected', [
    ('', 'Mix tapes'), ('#directions', 'Mix tapes'),
    ('#unlocks', 'Your unlocks'), ('#radio', 'Your unlocks'),
])
def test_home_recovery_preserves_default_and_valid_fragments(
        live_app, store, page, published_vibe_controls, fragment, selected, action_evidence):
    page.set_viewport_size({'width': 1440, 'height': 1000})
    # Controlled extension report; all Home templates, polling, navigation and
    # playback components run normally against the private live application.
    status = {
        'connected': True,
        'now_playing': {'title': 'Retained song', 'artist': 'Retained artist',
                        'video_id': 'retained-song', 'paused': True},
        'sensor_health': {'healthy': False, 'ytm_tabs': 1, 'responding_tabs': 0,
                          'recovering': True, 'reloaded_tabs': 1,
                          'error': 'internal-diagnostic-879: sensor did not answer'},
    }
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.goto(live_app + fragment)
    tab = page.get_by_role('tab', name=selected, exact=True)
    expect(tab).to_have_attribute('aria-selected', 'true')
    player = page.locator('.global-now-playing')
    expect(player.get_by_text('Retained song', exact=True)).to_be_visible()
    page.evaluate('window.sensorPlayer = document.querySelector(".global-now-playing")')
    before_tracks = page.locator('#home-mode-cards .gen-row').evaluate_all('rows => rows.map(row => row.dataset.key)')
    if selected == 'Mix tapes':
        expect(page.locator('#home-mode-cards .gen-card')).to_have_count(4)
        assert len(before_tracks) == 16
        assert len(set(before_tracks)) == 16
    writes = []
    page.on('request', lambda request: writes.append((request.method, request.url))
            if request.method not in ('GET', 'HEAD', 'OPTIONS') else None)
    for state, healthy, tabs, recovering in (
            ('recovering', False, 1, True), ('exhausted', False, 1, False),
            ('healthy', True, 1, False), ('no-tab', False, 0, False)):
        status['sensor_health'].update(healthy=healthy, ytm_tabs=tabs,
                                      responding_tabs=int(healthy), recovering=recovering,
                                      error='' if healthy or not tabs else 'internal-diagnostic-879: sensor did not answer')
        page.evaluate('window.playbackStatus.refresh()')
        assert page.evaluate('window.playbackStatus.snapshot.sensor_health') == status['sensor_health']
        assert_quiet_action(page)
        expect(page.locator('#home .notice.warning.banner:visible')).to_have_count(0)
        expect(page.locator('body')).not_to_contain_text('Reloading the YouTube Music tab')
        expect(page.locator('body')).not_to_contain_text('Reload the tab to try again')
        expect(page.locator('.onboard-card:visible')).to_have_count(0)
        expect(tab).to_have_attribute('aria-selected', 'true')
        expect(tab).to_be_enabled()
        expect(player.get_by_text('Retained song', exact=True)).to_be_visible()
        assert page.evaluate('window.sensorPlayer === document.querySelector(".global-now-playing")')
        assert page.locator('#home-mode-cards .gen-row').evaluate_all('rows => rows.map(row => row.dataset.key)') == before_tracks
        assert not writes
        if selected == 'Mix tapes':
            expect(page.get_by_role('button', name='Tune these mixes', exact=True)).to_be_enabled()
        if fragment == '':
            page.screenshot(path=action_evidence + '-sensor-' + state + '.png')
    page.evaluate("window.reconnectDocument = 'original'")
    mutations = []
    page.on('request', lambda request: mutations.append(request.url)
            if request.method == 'POST' and request.url.endswith('/home/vibe') else None)
    with page.expect_response(lambda response: response.request.method == 'GET'
                              and response.url == live_app + '/'):
        page.evaluate("window.dispatchEvent(Object.assign(new Event('app-request-failed'), "
                      "{detail: {url: '/home/vibe', method: 'POST', reconcile: true}}))")
    page.wait_for_function('!document.querySelector("main").hasAttribute("aria-busy")')
    expect(page.locator('#connection-error')).to_have_count(0)
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    expect(tab).to_have_attribute('aria-selected', 'true')
    expect(player.get_by_text('Retained song', exact=True)).to_be_visible()
    assert page.evaluate('window.sensorPlayer === document.querySelector(".global-now-playing")')
    assert page.evaluate('reconnectDocument') == 'original'
    assert not mutations
    if selected == 'Mix tapes':
        expect(page.get_by_role('button', name='Tune these mixes', exact=True)).to_be_visible()


def test_navigation_to_hashless_home_uses_its_default(
        live_app, store, page, published_vibe_controls):
    page.goto(live_app + '/playlists')
    page.evaluate("window.homeNavigationDocument = 'original'")
    page.locator('.topbar').get_by_role('link', name='Home', exact=True).click()
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_attribute('aria-selected', 'true')
    expect(page.get_by_role('button', name='Tune these mixes', exact=True)).to_be_visible()
    assert page.evaluate('homeNavigationDocument') == 'original'
