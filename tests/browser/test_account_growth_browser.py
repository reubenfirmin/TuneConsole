"""Rendered growth states and notification actions."""
import hashlib
from tests.enrichment_support import complete_entries, complete_discovered
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
from tests.browser.quiet_action_support import (  # noqa: F401 - shared evidence fixture
    action_evidence, assert_quiet_action, fail_action,
)


def growth_catalog(store):
    early_journey(store)
    catalog(store)
    complete_discovered(store, [row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], 1)


@pytest.fixture
def store(store, request):
    if request.node.originalname == 'test_festival_states_notices_home_choice_and_bonus_details':
        # This interaction-only journey repeatedly serves the same prepared
        # state. Keep ordinary private WAL reads instead of memory snapshots.
        return request.getfixturevalue('file_backed_store')
    return store




@pytest.mark.parametrize('width', [1536])
def test_festival_states_notices_home_choice_and_bonus_details(live_app, store, page, monkeypatch, width, action_evidence):
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
    page.goto(live_app + '/#unlocks', wait_until='domcontentloaded')
    page.screenshot(path=f'/tmp/tuneconsole-festival-initial-{width}.png', full_page=True)
    expect(page.get_by_role('heading', name='Your unlocks', exact=True)).to_be_visible()
    expect(page.get_by_role('tab', name='Your unlocks')).to_be_visible()
    expect(page.locator('.festival-destination.is-explored')).to_have_count(3)
    expect(page.locator('.festival-destination.is-ready')).to_have_count(3)
    expect(page.locator('.festival-destination.is-preparing')).to_have_count(1)
    expect(page.locator('.festival-destination.is-untouched')).to_have_count(7)
    assert page.locator('.festival-routes .growth-route').count() == 19
    expect(page.locator('.festival-routes .growth-route:visible')).to_have_count(11)
    expect(page.locator('.festival-routes .growth-route.is-preparing:visible')).to_have_count(1)
    assert page.locator('.festival-routes .growth-route:not(.is-explored):not(.is-ready):not(.is-connected):not(.is-preparing)').evaluate_all(
        'routes => routes.every(route => getComputedStyle(route).display === "none")')
    assert page.locator('.festival-routes path').evaluate_all(
        'paths => paths.every(path => getComputedStyle(path).strokeDasharray === "none")')
    assert page.locator('.festival-grounds').get_attribute('href') == '/static/festival-grounds-v4.png'
    expect(page.locator('.landmark-hint:visible')).to_have_count(1)
    # Prior successful use does not acknowledge either unlock notification.
    for _ in range(5):
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    expect(page.locator('.festival-destination.is-ready')).to_have_count(3)
    expect(page.locator('.landmark-hint:visible').filter(has_text='New')).to_have_count(0)
    page.get_by_role('button', name='Later', exact=True).click()
    expect(page.locator('.growth-unlock-notice')).to_be_hidden()
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('.growth-unlock-notice')).to_be_hidden()
    assert store.settings.get_setting('home_format') is None
    page.get_by_role('button', name='Choose my Home', exact=True).click()
    page.get_by_role('radio', name='Long play').check()
    expect(page.get_by_role('dialog', name='Choose what opens on Home')).to_be_visible()
    expect(page.get_by_role('radio', name='Long play')).to_be_checked()
    writes = []
    def not_saved(route):
        writes.append(route.request.post_data)
        fail_action(route, 'plaintext')
    page.route('**/home/growth/format', not_saved)
    save = page.get_by_role('button', name='Save Home choice', exact=True)
    save.click()
    expect(page.locator('.growth-home-choice').get_by_role('status')).to_have_text(
        'Choice not confirmed. Your selection is kept here.')
    expect(save).to_be_enabled()
    expect(page.get_by_role('radio', name='Long play')).to_be_checked()
    assert store.settings.get_setting('home_format') is None and len(writes) == 1
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '.png', full_page=True)
    page.unroute('**/home/growth/format', not_saved)
    old_growth = []
    def hold_old_growth(route):
        if not old_growth:
            response = route.fetch()
            assert response.ok
            old_growth.append((route, response.json()))
            page.evaluate('window.oldGrowthCaptured = true')
        else:
            route.continue_()
    page.route('**/home/growth', hold_old_growth)
    growth_model = 'Alpine.$data(document.querySelector(".home-growth"))'
    with page.expect_request('**/home/growth'):
        page.evaluate(f'void {growth_model}.refresh()')
    page.wait_for_function('window.oldGrowthCaptured === true')
    assert old_growth and old_growth[0][1]['home_choice'] is None
    def saved_but_lost(route):
        writes.append(route.request.post_data)
        assert route.fetch().status == 204
        route.abort()
    page.route('**/home/growth/format', saved_but_lost)
    save.click()
    expect(page.locator('.growth-home-choice')).to_be_hidden()
    assert store.settings.get_setting('home_format') == 'albums'
    assert writes == ['format=albums', 'format=albums']
    old_route, old_state = old_growth[0]
    old_route.fulfill(json=old_state)
    page.wait_for_function(f'!{growth_model}.refreshing')
    assert page.evaluate(f'{growth_model}.growth.home_choice') == 'albums'
    assert not page.evaluate(f'{growth_model}.growth.home_choice_available')
    page.unroute('**/home/growth', hold_old_growth)
    assert_quiet_action(page)
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
    # Exercise the actual SVG mask with every physical trail visible. Check
    # painted walking strips as well as foreground exclusions: a mask that
    # hides an entire bridge must fail even though its rails remain unchanged.
    svg = page.locator('.festival-map')
    routes = page.locator('.festival-routes')
    bus_turn = routes.locator('[data-destination="discover"] path').evaluate('''path => {
        const points = Array.from({length: Math.floor(path.getTotalLength()) - 8}, (_, i) => {
            const s = i + 4, a = path.getPointAtLength(s - 4),
                p = path.getPointAtLength(s), b = path.getPointAtLength(s + 4);
            const u = [p.x-a.x,p.y-a.y], v = [b.x-p.x,b.y-p.y];
            return {x:p.x,y:p.y,angle:Math.acos(Math.max(-1,Math.min(1,
                (u[0]*v[0]+u[1]*v[1])/(Math.hypot(...u)*Math.hypot(...v)))))};
        });
        const left = points.filter(p => p.x > 880 && p.x < 940 && p.y > 330);
        return {gap: left.reduce((a,b) => Math.abs(a.y-350) < Math.abs(b.y-350) ? a : b),
            bend: Math.max(...left.filter(p => p.y > 365 && p.y < 415).map(p => p.angle))};
    }''')
    # The painted lamp ends beside the bus's left edge. A walker passes through
    # that gap before sweeping around the forecourt, rather than turning at the display.
    assert 911 <= bus_turn['gap']['x'] <= 916, bus_turn
    assert bus_turn['bend'] < .7, bus_turn
    page.evaluate('''() => {
        Alpine.$data(document.querySelector('.home-growth')).refresh = async () => {};
        document.querySelectorAll('.growth-route').forEach(el => {
            el.style.setProperty('display','block','important');
            el.setAttribute('class','growth-route is-explored');
        });
    }''')
    page.set_viewport_size({'width': 3542, 'height': 2400})
    svg.screenshot(path='/tmp/tuneconsole-1161-current-open.png', animations='disabled')
    routes.locator('.growth-route').evaluate_all(
        "els => els.forEach(el => el.setAttribute('class','growth-route is-preparing'))")
    svg.screenshot(path='/tmp/tuneconsole-1161-current-preparing.png', animations='disabled')
    page.set_viewport_size({'width': width, 'height': 1100})
    page.evaluate('''() => {
        Alpine.$data(document.querySelector('.home-growth')).refresh = async () => {};
        document.querySelectorAll('.landmark-art').forEach(el => el.style.visibility='hidden');
        document.querySelectorAll('.growth-route').forEach(el => {
            el.style.setProperty('display','block','important');
            el.setAttribute('class','growth-route is-preparing');
        });
    }''')

    def snapshot():
        return Image.open(io.BytesIO(svg.screenshot(animations='disabled'))).convert('RGB')

    routes.evaluate("el => el.style.visibility='hidden'")
    dark = snapshot()
    routes.evaluate("el => el.style.visibility='visible'")
    preparing = snapshot()
    routes.evaluate("el => el.removeAttribute('mask')")
    unmasked = snapshot()
    routes.evaluate("el => el.setAttribute('mask','url(#festival-path-surface)')")
    routes.locator('.growth-route').evaluate_all(
        "els => els.forEach(el => el.setAttribute('class','growth-route is-explored'))")
    opened = snapshot()
    for name, shot in [('dark', dark), ('preparing', preparing), ('open', opened),
                       ('unmasked', unmasked)]:
        shot.save('/tmp/tuneconsole-1161-current-pixels-' + name + '.png')

    def delta(image, point, radius=0):
        px, py = round(point[0]*dark.width/1536), round(point[1]*dark.height/1024)
        return max(abs(a-b) for x in range(px-radius,px+radius+1)
                   for y in range(py-radius,py+radius+1)
                   for a,b in zip(dark.getpixel((x,y)),image.getpixel((x,y))))

    # Native artwork coordinates, including the bus bend that earlier visual
    # checks missed, both island bridges, and the cinema bank and audience.
    foreground = [(350,534),(427,505),(915,377),(472,511),(486,513),
                  (709,294),(961,198),(969,201),(1047,285),(1018,296),
                  (1073,267),(1194,316),(1363,272),(1449,420),
                  (1390,408),(1030,650),(650,760),(1347,765),
                  (1357,798),(1312,812),(1331,851),(1100,780),
                  (760,240),(1160,344),(1184,334),(451,388),
                  (474,390),(1252,323),(1199,314),(1296,790),(785,320),
                  (1298,744)]
    foreground_deltas = {p: (delta(preparing,p),delta(opened,p)) for p in foreground}
    assert not {p:d for p,d in foreground_deltas.items() if max(d)>4}
    assert delta(unmasked,(486,513))>10  # Negative control: the mask matters.
    walking = [(1025,290),(1040,655),(1080,672),(1350,369),(1440,430),
               (647,744),(1345,817),(1282,752),(424,455),(550,525)]
    walking_deltas = {p: (delta(opened,p,2),delta(preparing,p,2)) for p in walking}
    assert not {p:d for p,d in walking_deltas.items() if min(d)<=4}
    assert not errors
