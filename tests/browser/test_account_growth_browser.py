"""Rendered growth states, notification actions and live health counts."""
import hashlib
import inspect
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect
from PIL import Image

from tests.test_account_growth import catalog, ready_radio
from tests.onboarding_support import publish_personal_album, publish_personal_mix
from tests.test_growth_journey import early_journey
from tests.home_fixture_support import returning_listener
from yt_playlist.rec import account_growth


def growth_catalog(store):
    early_journey(store)
    catalog(store)
    store.discovery.mark_discovered_enriched([row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], 1)


def test_trail_light_preserves_foreground_scenery(live_app, store, page, monkeypatch):
    """Pixel regression on the real map/CSS with controlled all-route lighting.

    This is visual geometry coverage, not evidence of account readiness. The
    unmasked control proves the foreground samples would catch painted-over art.
    """
    actual_view = account_growth.view

    def explored_view(source, now, **kwargs):
        state = actual_view(source, now, **kwargs)
        for node in state['nodes']:
            node.update(state='explored', ready=True)
        return state

    monkeypatch.setattr(account_growth, 'view', explored_view)
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.set_viewport_size({'width': 1700, 'height': 1400})
    page.goto(live_app + '/#unlocks')
    routes = page.locator('.festival-routes')
    expect(routes.locator('.growth-route:visible')).to_have_count(14)
    page.mouse.move(0, 0)
    # Isolate paint from notices and asynchronous polling. Both light treatments
    # below use the shipped template, filter, mask, artwork and stylesheets.
    page.evaluate('''() => {
        const state = Alpine.$data(document.querySelector('.home-growth'));
        state.refresh = async () => {};
        document.querySelectorAll('.growth-unlock-notice,.growth-home-choice').forEach(el => el.style.setProperty('display','none','important'));
    }''')
    svg = page.locator('.festival-map')
    svg.screenshot(path='/tmp/tuneconsole-611-trails-explored.png', animations='disabled')
    # Lit venue cutouts also sit above the paths. Hide that layer during pixel
    # comparison to exercise the path mask even when those venues are unlit.
    page.locator('.landmark-art').evaluate_all("els => els.forEach(el => el.style.visibility = 'hidden')")

    def snapshot():
        return Image.open(io.BytesIO(svg.screenshot(animations='disabled'))).convert('RGB')

    routes.evaluate("el => el.style.visibility = 'hidden'")
    dark = snapshot()
    routes.evaluate("el => el.style.visibility = 'visible'")
    explored = snapshot()
    routes.locator('.growth-route').evaluate_all("els => els.forEach(el => el.setAttribute('class','growth-route is-preparing'))")
    lit = snapshot()
    routes.evaluate("el => el.removeAttribute('mask')")
    unmasked = snapshot()
    routes.evaluate("el => el.setAttribute('mask','url(#festival-path-surface)')")
    page.locator('.landmark-art').evaluate_all("els => els.forEach(el => el.style.visibility = 'visible')")
    svg.screenshot(path='/tmp/tuneconsole-611-trails-preparing.png', animations='disabled')

    def difference(first, second, point, *, radius=0):
        px = round(point[0] * first.width / 1536)
        py = round(point[1] * first.height / 1024)
        return max(abs(a - b)
                   for x in range(px - radius, px + radius + 1)
                   for y in range(py - radius, py + radius + 1)
                   for a, b in zip(first.getpixel((x, y)), second.getpixel((x, y))))

    foreground = {
        'entrance fir': (136, 503),
        'small fir crown': (169, 510),
        'small fir projecting branch': (164, 517),
        'second fir crown': (194, 509),
        'second fir projecting branch': (198, 519),
        'bank shrub tip': (216, 520),
        'bank leaf': (222, 525),
        'fork lamp left roof': (346, 521),
        'fork lamp lower head': (350, 534),
        'forecourt lamp': (427, 505),
        'camper roof': (1008, 311),
        'reading-walk fir': (647, 841),
        'cinema screen': (1100, 780),
        'waterfall handrail': (1335, 374),
        'upper switchback lamp post': (789, 321),
        'outer switchback lamp post': (829, 327),
        'first bridge lamp post': (1329, 348),
        'middle bridge lamp post': (1370, 372),
        'last bridge lamp post': (1422, 405),
        'stage approach visitor': (1246, 623),
        'stage forecourt lamp': (1297, 670),
    }
    evidence = {name: {'unmasked': difference(dark, unmasked, point),
                       'masked': difference(dark, lit, point),
                       'explored': difference(dark, explored, point)}
                for name, point in foreground.items()}
    assert all(item['unmasked'] > 4 for item in evidence.values()), json.dumps(evidence)
    assert all(item['masked'] <= 2 for item in evidence.values()), json.dumps(evidence)
    assert all(item['explored'] <= 4 for item in evidence.values()), json.dumps(evidence)
    # These independent points follow the artwork's actual stair treads, not
    # coordinates sampled from the route definition. They catch the old shortcut
    # through the planting bed and the displaced upper stair bend.
    for point in [(734, 402), (761, 446), (823, 350)]:
        assert difference(dark, explored, point, radius=2) > 4, f'Explored trail misses stair tread at {point}'
        assert difference(dark, lit, point, radius=2) > 4, f'Preparing trail misses stair tread at {point}'
    for point in [(155, 503), (174, 514), (200, 517), (250, 530), (1058, 260), (1128, 361), (686, 800), (785, 925)]:
        assert difference(dark, lit, point) > 4, f'Adjacent walking surface lost its light at {point}'


@pytest.mark.parametrize('width', [1536])
def test_festival_states_notices_home_choice_and_bonus_details(live_app, store, page, monkeypatch, width):
    growth_catalog(store)
    publish_personal_album(store)
    publish_personal_mix(store)
    with store.conn:
        store.conn.execute('UPDATE recordings SET first_enriched_at=1')
    store.settings.set_setting('onboard_dismissed', '1')
    store.history.record_play_event(1, 'song 0|artist', 'video-0', 1)
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'episodes': {'mixes': 1, 'albums': 1, 'radio': 0}}}, 1)
    actual_view = account_growth.view
    # Isolate the view's interactions from worker preparation timing. Counts and
    # prior use still come from the isolated DB; these three offers are controlled.
    def ready_view(source, now, **_kwargs):
        return actual_view(source, now, mixes_ready=True, albums_ready=True, radio=ready_radio())
    monkeypatch.setattr(account_growth, 'view', ready_view)
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(status=404))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1100})
    page.goto(live_app + '/#unlocks')
    page.screenshot(path=f'/tmp/tuneconsole-festival-initial-{width}.png', full_page=True)
    expect(page.get_by_role('heading', name='Your unlocks', exact=True)).to_be_visible()
    expect(page.get_by_role('tab', name='Your unlocks')).to_be_visible()
    expect(page.locator('.festival-destination.is-explored')).to_have_count(3)
    expect(page.locator('.festival-destination.is-ready')).to_have_count(3)
    expect(page.locator('.festival-destination.is-preparing')).to_have_count(1)
    expect(page.locator('.festival-destination.is-untouched')).to_have_count(7)
    assert page.locator('.festival-routes .growth-route').count() == 14
    expect(page.locator('.festival-routes .growth-route:visible')).to_have_count(4)
    expect(page.locator('.festival-routes .growth-route.is-preparing:visible')).to_have_count(1)
    assert page.locator('.festival-routes .growth-route:not(.is-explored):not(.is-preparing)').evaluate_all(
        'routes => routes.every(route => getComputedStyle(route).display === "none")')
    assert page.locator('.festival-routes path').evaluate_all(
        'paths => paths.every(path => getComputedStyle(path).strokeDasharray === "none")')
    assert page.locator('.festival-grounds').get_attribute('href') == '/static/festival-grounds-v4.png'
    expect(page.locator('.landmark-hint:visible').filter(has_text='New')).to_have_count(5)
    # Prior successful use does not acknowledge either unlock notification.
    for _ in range(5):
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    expect(page.locator('.festival-destination.is-ready')).to_have_count(3)
    expect(page.locator('.landmark-hint:visible').filter(has_text='New')).to_have_count(0)
    page.get_by_role('button', name='Later', exact=True).click()
    expect(page.locator('.growth-unlock-notice')).to_be_hidden()
    page.reload()
    expect(page.locator('.growth-unlock-notice')).to_be_hidden()
    assert store.settings.get_setting('home_format') is None
    page.get_by_role('button', name='Choose my Home', exact=True).click()
    page.get_by_role('radio', name='Long play').check()
    expect(page.get_by_role('dialog', name='Choose what opens on Home')).to_be_visible()
    expect(page.get_by_role('radio', name='Long play')).to_be_checked()
    page.get_by_role('button', name='Save Home choice', exact=True).click()
    expect(page.locator('.growth-home-choice')).to_be_hidden()
    assert store.settings.get_setting('home_format') == 'albums'
    bonus = page.get_by_role('button', name=re.compile('^Richer music details,'))
    bonus.scroll_into_view_if_needed()
    hit = bonus.evaluate('''button => {
        const box = button.getBoundingClientRect();
        const target = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return {clickable: button.contains(target), target: target?.outerHTML.slice(0, 180),
            pointer: getComputedStyle(button).pointerEvents, parent: button.parentElement.className,
            box: box.toJSON(), map: button.closest('.festival-map').getBoundingClientRect().toJSON()};
    }''')
    assert hit['clickable'], json.dumps(hit)
    bonus.click()
    expect(page.get_by_role('heading', name='Find more music that fits')).to_be_visible()
    expect(page.get_by_text('Get a Last.fm key:', exact=False)).to_be_hidden()
    page.get_by_text('See the setup steps', exact=True).filter(visible=True).click()
    expect(page.get_by_text('Song and artist names are sent to Last.fm', exact=False)).to_be_visible()
    assert store.settings.get_setting('home_format') == 'albums'
    assert page.locator('body').evaluate('(body) => body.scrollWidth <= innerWidth')
    assert page.locator('.growth-detail h2').evaluate('(el) => getComputedStyle(el).textAlign') == 'left'
    page.screenshot(path=f'/tmp/tuneconsole-festival-{width}.png', full_page=True)
    page.get_by_role('button', name='Close destination', exact=True).click()
    for label in ('Your past listening', 'Richer music details', 'Stories behind the records'):
        destination = page.get_by_role('button', name=re.compile('^' + re.escape(label) + ','))
        destination.locator('.landmark-label').click()
        page.get_by_role('button', name='Back to the festival', exact=True).click()
        expect(page.locator('.growth-detail')).not_to_be_visible()
        expect(destination).to_have_class(re.compile(r'\bis-untouched\b'))
        assert page.url.endswith('#unlocks')
        assert store.settings.get_setting('home_format') == 'albums'
    assert not errors


@pytest.mark.parametrize('width', [1536])
def test_successful_use_keeps_unlock_new_until_explicitly_seen(live_app, store, page, monkeypatch, width):
    from yt_playlist.rec import choice_evidence

    growth_catalog(store)
    publish_personal_mix(store, now=1)
    publish_personal_album(store, now=1)
    store.enrichment_queue.mark_enriched([row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1)
    actual_view = account_growth.view
    monkeypatch.setattr(account_growth, 'view', lambda source, now, **_kwargs:
                        actual_view(source, now, mixes_ready=True, albums_ready=True, radio=ready_radio()))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.set_viewport_size({'width': width, 'height': 1100})
    page.goto(live_app + '/#unlocks')
    radio = page.locator('.festival-destination').filter(has_text='On air')
    expect(radio).to_have_class(re.compile(r'\bis-ready\b'))
    expect(radio.locator('.landmark-hint')).to_contain_text('New')
    # Earlier main-path unlocks are announced first, including ones already
    # explored. Reading them is separate from recording successful Radio use.
    while page.locator('.growth-unlock-copy strong').inner_text() != 'On air':
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    # Controlled deliberate choice/start through the real evidence projection.
    offer = choice_evidence.issue(store, 'radio', [{'id': 'station'}], .1, context={})
    choice_evidence.respond(store, offer, 'choice', .2, option_id='station')
    choice_evidence.respond(store, offer, 'start', .3, option_id='station')
    choice_evidence.compile_report(store, 1)
    page.evaluate('() => Alpine.$data(document.querySelector(".home-growth")).refresh()')
    expect(radio).to_have_class(re.compile(r'\bis-explored\b'))
    expect(radio.locator('.landmark-hint')).to_contain_text('New')
    page.reload()
    expect(radio).to_have_class(re.compile(r'\bis-explored\b'))
    expect(radio.locator('.landmark-hint')).to_contain_text('New')
    page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    expect(radio.locator('.landmark-hint')).not_to_contain_text('New')
    expect(radio).to_have_class(re.compile(r'\bis-explored\b'))
    listening = page.get_by_role('button', name='Mix tapes, Ready to try', exact=True)
    expect(listening).to_have_class(re.compile(r'\bis-ready\b'))
    for _ in range(page.evaluate('Alpine.$data(document.querySelector(".home-growth")).newNodes.length')):
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    expect(listening).to_have_class(re.compile(r'\bis-ready\b'))
    page.reload()
    expect(page.locator('.landmark-hint:visible').filter(has_text='New')).to_have_count(0)
    assert store.settings.get_setting('home_format') is None


@pytest.mark.parametrize('width', [1536])
def test_unlock_notice_is_a_corner_popup_and_maintenance_keeps_health_in_place(live_app, store, page, monkeypatch, width):
    growth_catalog(store)
    publish_personal_mix(store, now=1)
    publish_personal_album(store, now=1)
    actual_view = account_growth.view
    monkeypatch.setattr(account_growth, 'view', lambda source, now, **_kwargs:
                        actual_view(source, now, mixes_ready=True, albums_ready=True, radio=ready_radio()))
    maintenance = {'active': False, 'warning': False, 'message': 'Preparing listening history.'}
    page.route('**/storage-maintenance/status', lambda route: route.fulfill(json=maintenance))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/#directions')
    star = page.get_by_role('tab', name='Your unlocks')
    expect(star).to_have_class(re.compile(r'\bhas-new\b'))
    expect(star).to_have_class(re.compile(r'\bis-twinkling\b'))
    assert star.locator('svg').evaluate('el => getComputedStyle(el).animationName') == 'growth-twinkle'
    expect(page.locator('.growth-tab-dot')).to_have_count(0)
    notice = page.locator('.growth-unlock-notice')
    expect(notice).to_be_visible()
    assert notice.evaluate('el => getComputedStyle(el).position') == 'fixed'
    page.wait_for_function('''() => {
        const box = document.querySelector('.growth-unlock-notice').getBoundingClientRect();
        return Math.abs(innerWidth - box.right - 24) < 1
            && Math.abs(innerHeight - box.bottom - 24) < 1;
    }''')
    box = notice.bounding_box()
    assert box['x'] + box['width'] <= width and box['x'] >= 0
    assert box['y'] > 250
    assert page.locator('.account-health').evaluate('el => !!el.closest("#account-health-slot") && !!el.closest(".topbar")')
    assert page.locator('.account-health').bounding_box()['x'] < 50
    health_before = page.locator('.account-health').bounding_box()
    health_position = page.locator('.account-health').evaluate('el => getComputedStyle(el).position')
    maintenance['active'] = True
    maintenance['banner_message'] = 'Updating library storage; browsing and playback remain available.'
    banner = page.locator('#storage-maintenance')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "true"')
    expect(banner).to_have_text('Updating library storage; browsing and playback remain available.')
    expect(banner).to_be_visible()
    dock = page.locator('.account-health')
    expect(dock).to_be_visible()
    assert dock.evaluate('el => getComputedStyle(el).position') == health_position
    health_after = dock.bounding_box()
    assert all(abs(health_after[key] - health_before[key]) < 1 for key in ('x', 'width', 'height'))
    # The fixed desktop rail stays put while the notice appears.
    assert abs(health_after['y'] - health_before['y']) < 1
    assert page.locator('.global-now-playing-slot').count() == 1
    page.screenshot(path=f'/tmp/tuneconsole-561-corner-popup-{width}.png', full_page=True)
    expect(star).not_to_have_class(re.compile(r'\bis-twinkling\b'))
    assert store.settings.get_setting('home_format') is None


def test_corner_unlock_opens_a_destination_without_leaving_mix_tapes(live_app, store, page, monkeypatch, tmp_path):
    """The corner notice can open the map's dialog while its tab is hidden."""
    growth_catalog(store)
    publish_personal_mix(store, now=1)
    publish_personal_album(store, now=1)
    store.enrichment_queue.mark_enriched([row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1)
    store.settings.set_setting('home_format', 'albums')
    actual_view = account_growth.view
    monkeypatch.setattr(account_growth, 'view', lambda source, now, **_kwargs:
                        actual_view(source, now, mixes_ready=True, albums_ready=True, radio=ready_radio()))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(status=404))
    page.set_viewport_size({'width': 1536, 'height': 1000})
    static = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static'
    assets = {name: (static / name).read_bytes() for name in ('account-growth.js', 'account-growth.css')}
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append((request.method, urlsplit(request.url).path)))
    report = {'controlled_readiness': True, 'status': 'incomplete', 'served_assets': {}}
    page.add_init_script('''localStorage.setItem('tc-growth-notices-v1', JSON.stringify({
        seen: ['foundation', 'albums', 'discover', 'listening'], later: false}));''')
    try:
        with page.expect_response(lambda response: urlsplit(response.url).path == '/static/account-growth.js') as js_response:
            with page.expect_response(lambda response: urlsplit(response.url).path == '/static/account-growth.css') as css_response:
                page.goto(live_app + '/#directions')
        for response, name in ((js_response.value, 'account-growth.js'), (css_response.value, 'account-growth.css')):
            assert response.status == 200 and response.body() == assets[name]
            report['served_assets'][name] = hashlib.sha256(response.body()).hexdigest()
        tab = page.get_by_role('tab', name='Mix tapes', exact=True)
        expect(tab).to_have_attribute('aria-selected', 'true')
        expect(page.locator('[data-dash="unlocks"]')).to_be_hidden()
        notice = page.locator('.growth-unlock-notice')
        expect(notice.locator('.growth-unlock-copy strong')).to_have_text('On air')
        before_url = page.url
        assert store.settings.get_setting('home_format') == 'albums'

        def unchanged_selection():
            expect(tab).to_have_attribute('aria-selected', 'true')
            assert page.url == before_url
            assert store.settings.get_setting('home_format') == 'albums'

        notice.get_by_role('button', name='See what’s new', exact=True).click()
        popup = page.get_by_role('dialog', name='On air', exact=True)
        expect(popup).to_be_visible()
        expect(popup.get_by_role('button', name='Open On air', exact=True)).to_be_visible()
        unchanged_selection()
        page.screenshot(path=tmp_path / 'corner-unlock-destination-1536.png', full_page=True)
        popup.get_by_role('button', name='Close destination', exact=True).click()
        expect(popup).not_to_be_visible()
        expect(notice).to_be_visible()
        unchanged_selection()
        assert not [(method, path) for method, path in requests if method == 'POST' and
                    path in ('/home/growth/format', '/play', '/radio/start')]
        assert not errors
        report['status'] = 'passed'
    finally:
        report.update(url=page.url, home_format=store.settings.get_setting('home_format'),
                      requests=requests, page_errors=errors)
        try:
            report['destination_dialog'] = page.locator('.growth-detail').evaluate('''dialog => {
                const growth = dialog.closest('.home-growth');
                const state = growth && Alpine.$data(growth);
                const ancestors = [];
                for (let node = dialog; node && ancestors.length < 10; node = node.parentElement) {
                    const style = getComputedStyle(node);
                    ancestors.push({tag: node.tagName, id: node.id, class: node.className,
                        display: style.display, visibility: style.visibility,
                        rect: node.getBoundingClientRect().toJSON()});
                }
                return {open: dialog.open, modal: dialog.matches(':modal'),
                    connected: dialog.isConnected, title: dialog.querySelector('h2')?.textContent,
                    selected: state?.selected, selectedNode: state?.selectedNode?.id,
                    popupMatches: state?.destinationPopup === dialog, ancestors};
            }''', timeout=2000)
        except Exception as error:
            report['destination_diagnostic_error'] = str(error)
        if report['status'] != 'passed':
            try:
                page.screenshot(path=tmp_path / 'corner-unlock-destination-failure-1536.png',
                                full_page=True, timeout=3000)
            except Exception as error:
                report['failure_screenshot_error'] = str(error)
        (tmp_path / 'corner-unlock-destination.json').write_text(json.dumps(report, indent=2))
        assert all((static / name).read_bytes() == body for name, body in assets.items())


@pytest.mark.parametrize('width', [1536])
def test_festival_landmarks_open_popups_and_only_rendered_paths_have_a_legend(live_app, store, page, monkeypatch, width):
    growth_catalog(store)
    publish_personal_mix(store, now=1)
    publish_personal_album(store, now=1)
    # This popup exercises retained Vibes access. Current output alone cannot
    # skip the serial TV predecessor (#608/#649); an earlier Vibes listen keeps
    # access independently of today's preparation.
    returning_listener(store, 'vibes', now=1)
    store.enrichment_queue.mark_enriched([row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1)
    actual_view = account_growth.view
    explored = [False]

    def ready_view(source, now, **_kwargs):
        state = actual_view(source, now, mixes_ready=True, albums_ready=True, radio=ready_radio())
        # Controlled availability for the Vibes popup and uniform-path legend.
        for node in state['nodes']:
            if node['id'] == 'vibes' or explored[0]:
                node.update(ready=True, state='explored' if explored[0] else 'ready', status='Ready to try')
            if node['id'] == 'vibes':
                node['href'] = '/#directions'
        state['vibes_ready'] = True
        return state

    monkeypatch.setattr(account_growth, 'view', ready_view)
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/#unlocks')
    for _ in range(page.evaluate('Alpine.$data(document.querySelector(".home-growth")).newNodes.length')):
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    page.get_by_role('button', name='Later', exact=True).click()
    expect(page.locator('.growth-detail')).not_to_be_visible()
    expect(page.locator('.growth-strength')).to_have_count(0)
    expect(page.get_by_text('More ways to listen.', exact=False)).to_have_count(0)
    expect(page.get_by_text('Preparation counts music details', exact=False)).to_have_count(0)
    expect(page.get_by_role('button', name=re.compile('First sounds'))).to_have_count(0)
    map_box = page.locator('.festival-map-scroll').bounding_box()
    panel_box = page.locator('[data-dash="unlocks"]').bounding_box()
    assert abs(map_box['width'] - panel_box['width']) < 2
    assert abs(map_box['y'] - panel_box['y']) < 2
    tabs = page.locator('.dash-navigation').bounding_box()
    assert abs(map_box['y'] - tabs['y'] - tabs['height']) < 2
    assert page.locator('.festival-routes .growth-route:not(.is-explored):not(.is-preparing)').evaluate_all(
        'routes => routes.every(route => getComputedStyle(route).display === "none")')
    for label in ('Mix tapes', 'Long play', 'On air'):
        stop = page.get_by_role('button', name=label + ', Ready to try', exact=True)
        stop.locator('.landmark-hit').click()
        popup = page.get_by_role('dialog', name=label, exact=True)
        expect(popup).to_be_visible()
        expect(popup.get_by_role('button', name='Open ' + label, exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        expect(popup).not_to_be_visible()
    vibes = page.get_by_role('button', name='Vibes, Ready to try', exact=True)
    vibes.locator('.landmark-hit').hover()
    expect(page.locator('.landmark-art.is-lit')).to_have_count(1)
    vibes.locator('.landmark-hit').click()
    popup = page.get_by_role('dialog', name='Set the mood.', exact=True)
    expect(popup).to_be_visible()
    expect(popup.get_by_text('Unlocked in Mix tapes', exact=True)).to_be_visible()
    expect(popup.get_by_role('button', name='Open Mix tapes', exact=True)).to_be_visible()
    assert not re.search(r'\b\d+\b|measured|predicted', popup.inner_text())
    assert popup.locator('h2').evaluate('el => getComputedStyle(el).textAlign') == 'left'
    popup.evaluate('el => Promise.allSettled(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))')
    page.screenshot(path=f'/tmp/tuneconsole-562-vibes-popup-{width}.png', full_page=True)
    popup.get_by_role('button', name='Open Mix tapes', exact=True).click()
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_attribute('aria-selected', 'true')
    page.get_by_role('tab', name='Your unlocks').click()
    explored[0] = True
    page.evaluate('() => Alpine.$data(document.querySelector(".home-growth")).refresh()')
    expect(page.locator('.growth-legend li:visible')).to_have_count(1)
    expect(page.locator('.growth-legend .is-explored')).to_be_visible()
    expect(page.locator('.festival-routes .growth-route:visible')).to_have_count(14)
    assert page.locator('.festival-routes path').evaluate_all(
        'paths => paths.every(path => getComputedStyle(path).strokeDasharray === "none")')
    page.locator('main').evaluate('el => Promise.allSettled(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))')
    page.screenshot(path=f'/tmp/tuneconsole-562-festival-map-{width}.png', full_page=True)
    assert page.locator('body').evaluate('body => body.scrollWidth <= innerWidth')


def test_health_refresh_and_poll_preserve_the_selected_listening_tab(live_app, store, page, tmp_path):
    """Controlled owned songs and a real prepared mix; no readiness overrides."""
    root = Path(__file__).resolve().parents[2]
    source_paths = [root / name for name in (
        'src/yt_playlist/rec/account_growth.py',
        'src/yt_playlist/web/routes/home_growth.py',
        'src/yt_playlist/web/templates/home.html',
        'src/yt_playlist/web/templates/_partials/account_health.html',
        'src/yt_playlist/web/static/account-growth.js',
        'src/yt_playlist/web/static/account-growth.css',
    )]
    sources = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in source_paths}
    assert Path(inspect.getsourcefile(account_growth.view)).resolve() == source_paths[0]
    publish_personal_mix(store, now=1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_setup_complete', '1')
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'episodes': {'mixes': 1}}}, 1)
    initial_entries = [row[0] for row in store.conn.execute(
        "SELECT id FROM catalog_entries WHERE video_id IN ('prepared-0','prepared-1')")]
    assert len(initial_entries) == 2
    store.enrichment_queue.mark_enriched(initial_entries, 1)
    initial = account_growth.view(store, 1)
    assert initial['ready_formats']['mixes'], 'Controlled published mix must be genuinely usable'
    assert tuple(initial['health'][key] for key in ('processed', 'total', 'percent')) == (2, 16, 12)
    errors, requests, snapshots = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append((request.method, urlsplit(request.url).path)))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(status=404))
    page.set_viewport_size({'width': 1536, 'height': 900})
    report = {'status': 'incomplete', 'source_before': sources,
              'qualification': 'Controlled four prepared mixes with 16 owned songs and additions; '
                               'real projection, GET refresh and natural 15-second poll.'}
    try:
        with page.expect_response(lambda response: urlsplit(response.url).path == '/static/account-growth.js') as js_response:
            with page.expect_response(lambda response: urlsplit(response.url).path == '/static/account-growth.css') as css_response:
                page.goto(live_app + '/#directions')
        for response, name in ((js_response.value, 'account-growth.js'),
                               (css_response.value, 'account-growth.css')):
            expected = (root / 'src/yt_playlist/web/static' / name).read_bytes()
            assert response.status == 200 and response.body() == expected
        report['served_assets_match'] = True
        strip = page.locator('.account-health')
        tab = page.get_by_role('tab', name='Mix tapes', exact=True)

        def visible_health(processed, total, percent):
            expect(strip).to_be_visible()
            expect(strip.get_by_text(f'{processed} of {total} songs', exact=True)).to_be_visible()
            expect(strip.get_by_text(f'{percent}%', exact=True)).to_be_visible()
            expect(strip.get_by_role('progressbar', name='Catalog preparation')).to_have_attribute('value', str(percent))
            expect(tab).to_have_attribute('aria-selected', 'true')
            expect(page.locator('[data-dash="directions"]')).to_be_visible()
            assert urlsplit(page.url).fragment == 'directions'
            assert strip.evaluate("el => !el.closest('[role=tabpanel]') && !el.closest('.home-dashboard')")
            assert strip.evaluate('''el => {
                const box = el.getBoundingClientRect();
                return !!el.closest('.topbar') && box.width > 0
                    && box.left >= 0 && box.right <= innerWidth;
            }''')

        visible_health(2, 16, 12)
        page.screenshot(path=str(tmp_path / 'health-initial-1536.png'), full_page=True)
        with store._lock:
            entry = store.tracks.upsert_track('health-added', 'Health addition', 'Artist', None, 200)
        with page.expect_response(lambda response: urlsplit(response.url).path == '/home/growth') as refreshed:
            page.evaluate("() => Alpine.$data(document.querySelector('.home-growth')).refresh()")
        assert refreshed.value.status == 200
        snapshots.append(refreshed.value.json()['health'])
        visible_health(2, 17, 11)
        with store._lock:
            store.enrichment_queue.mark_enriched([entry], 2)
        # Observe the existing production timer without clock changes, sleeps,
        # alternate response payloads, or direct DOM/data mutation.
        with page.expect_response(lambda response: urlsplit(response.url).path == '/home/growth', timeout=20000) as polled:
            pass
        assert polled.value.status == 200
        snapshots.append(polled.value.json()['health'])
        visible_health(3, 17, 17)
        page.screenshot(path=str(tmp_path / 'health-updated-1536.png'), full_page=True)
        # Home reads its normal menu even when health is below Radio's unlock
        # threshold. Permit those GETs while retaining the no-action boundary.
        report['unexpected_action_requests'] = [
            (method, path) for method, path in requests
            if method not in ('GET', 'HEAD', 'OPTIONS')
            and path in ('/radio/seeds', '/home/generate', '/home/tv/play')]
        assert not report['unexpected_action_requests']
        assert not errors
        assert {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_paths} == sources
        report['status'] = 'passed'
    finally:
        report.update(page_errors=errors, health_snapshots=snapshots, requests=requests,
                      source_after={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in source_paths})
        if report['source_after'] != sources:
            report['status'] = 'failed_source_custody'
        (tmp_path / 'health-live-update.json').write_text(json.dumps(report, indent=2))
        assert report['source_after'] == sources
