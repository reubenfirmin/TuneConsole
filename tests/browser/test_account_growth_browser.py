"""Rendered growth states and notification actions."""
from tests.enrichment_support import complete_entries, complete_discovered
import json
import re

import pytest
from playwright.sync_api import expect

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
def test_festival_states_notices_home_choice_and_bonus_details(
        live_app, store, page, monkeypatch, width, action_evidence, record_testsuite_property):
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
    # Every popup explains its actual next step, without disclosure controls.
    # Earned access hides the instructions even while fresh output prepares.
    popup = page.locator('.growth-detail')
    current_nodes = page.evaluate(f'{growth_model}.growth.nodes')
    for item in current_nodes:
        if item['id'] == 'first':
            continue
        destination = page.get_by_role('button', name=re.compile('^' + re.escape(item['label']) + ','))
        destination.locator('.landmark-label').click()
        expect(popup.locator('.growth-status')).to_have_text(item['status'])
        expect(popup.locator('details, summary')).to_have_count(0)
        guidance = popup.get_by_role('region', name='How to get here')
        accessible = item['ready'] if item['bonus'] else item['unlocked'] and bool(item['href'])
        if accessible:
            expect(guidance).to_be_hidden()
        else:
            expect(guidance).to_be_visible()
            expect(guidance).to_contain_text(item['how_to'])
        if item['href']:
            expect(popup.locator('a.btn-primary').last).to_have_attribute('href', item['href'])
        if item['id'] == 'deepseek':
            popup.evaluate('el => Promise.all(el.getAnimations().map(animation => animation.finished))')
            page.screenshot(path=f'/tmp/tuneconsole-festival-deepseek-{width}.png', full_page=True)
        page.get_by_role('button', name='Close destination', exact=True).click()
    page.get_by_role('button', name=re.compile('^Vibes,')).locator('.landmark-label').click()
    expect(popup.get_by_role('region', name='How to get here')).to_contain_text(
        'Play a variety of music in YouTube Music with TuneConsole open.')
    popup.evaluate('el => Promise.all(el.getAnimations().map(animation => animation.finished))')
    page.screenshot(path=f'/tmp/tuneconsole-festival-vibes-{width}.png', full_page=True)
    page.get_by_role('button', name='Close destination', exact=True).click()
    page.get_by_role('button', name=re.compile('^Long play,')).click()
    expect(popup.locator('.growth-status')).to_have_text('Explored')
    expect(popup.locator('.growth-status')).to_have_class(re.compile(r'\btag\b'))
    page.get_by_role('link', name='Open Long play', exact=True).click()
    expect(page.get_by_role('tab', name='Long play', exact=True)).to_have_attribute('aria-selected', 'true')
    expect(popup).to_be_hidden()
    page.get_by_role('tab', name='Your unlocks', exact=True).click()
    page.evaluate(f'''() => {{
        const destination = {growth_model}.node('tv');
        window.originalTv = {{...destination}};
        Object.assign(destination, {{unlocked: true, ready: false, usable: false, state: 'preparing', href: '/#tv'}});
    }}''')
    page.wait_for_function(f'!{growth_model}.refreshing')
    with page.expect_response('**/home/growth') as refreshed:
        page.evaluate(f'async () => {{ await {growth_model}.refresh(); }}')
    record_testsuite_property('tv-after-refresh', json.dumps({
        'server': next(node for node in refreshed.value.json()['nodes'] if node['id'] == 'tv'),
        'browser': page.evaluate(f'{growth_model}.node("tv")'),
    }))
    page.get_by_role('button', name=re.compile('^TV,')).click()
    expect(popup.get_by_role('region', name='How to get here')).to_be_hidden()
    expect(popup.get_by_role('link', name='Open TV', exact=True)).to_be_visible()
    page.get_by_role('button', name='Close destination', exact=True).click()
    page.evaluate(f'Object.assign({growth_model}.node("tv"), window.originalTv)')
    bonus = page.get_by_role('button', name=re.compile('^Richer music details,'))
    bonus_label = bonus.locator('.landmark-label')
    bonus_label.scroll_into_view_if_needed()
    hit = bonus_label.evaluate('''label => {
        const button = label.closest('.festival-destination');
        const box = label.getBoundingClientRect();
        const target = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return {clickable: button.contains(target), target: target?.outerHTML.slice(0, 180),
            pointer: getComputedStyle(button).pointerEvents, parent: button.parentElement.className,
            box: box.toJSON(), map: button.closest('.festival-map').getBoundingClientRect().toJSON()};
    }''')
    assert hit['clickable'], json.dumps(hit)
    bonus_label.click()
    expect(page.get_by_role('heading', name='Find more music that fits')).to_be_visible()
    expect(popup.get_by_role('region', name='How to get here')).to_contain_text('Register for a free Last.fm API key')
    expect(popup).to_contain_text('Last.fm has better genre labels than the free, public sources we use by default.')
    expect(page.get_by_text('To find genres, TuneConsole sends Last.fm song and artist names.', exact=False)).to_be_visible()
    assert store.settings.get_setting('home_format') == 'albums'
    assert page.locator('body').evaluate('(body) => body.scrollWidth <= innerWidth')
    assert page.locator('.growth-detail h2').evaluate('(el) => getComputedStyle(el).textAlign') == 'left'
    page.screenshot(path=f'/tmp/tuneconsole-festival-{width}.png', full_page=True)
    page.evaluate(f'{growth_model}.node("lastfm").ready = true')
    expect(popup.get_by_role('region', name='How to get here')).to_be_hidden()
    page.evaluate(f'{growth_model}.node("lastfm").ready = false')
    page.get_by_role('link', name='Set up Last.fm', exact=True).click()
    expect(page.get_by_role('tab', name='Enrichment', exact=True)).to_have_attribute('aria-selected', 'true')
    expect(page.locator('.provider-card[data-name="lastfm"]')).to_contain_text(
        'Register for a free API key to improve recommendations over time.')
    page.goto(live_app + '/#unlocks', wait_until='domcontentloaded')
    bonus.locator('.landmark-hit').click()
    page.get_by_role('button', name='Close destination', exact=True).click()
    for label in ('Your past listening', 'Richer music details', 'Stories behind the records'):
        destination = page.get_by_role('button', name=re.compile('^' + re.escape(label) + ','))
        destination.locator('.landmark-label').click()
        page.get_by_role('button', name='Back to the festival', exact=True).click()
        expect(page.locator('.growth-detail')).not_to_be_visible()
        expect(destination).to_have_class(re.compile(r'\bis-untouched\b'))
        assert page.url.endswith('#unlocks')
        assert store.settings.get_setting('home_format') == 'albums'
    # Render both trail treatments for visual inspection. Point samples cannot
    # establish whether a route follows the artwork or masks its foreground.
    svg = page.locator('.festival-map')
    routes = page.locator('.festival-routes')
    page.evaluate('''() => {
        Alpine.$data(document.querySelector('.home-growth')).refresh = async () => {};
        document.querySelectorAll('.growth-route').forEach(el => {
            el.style.setProperty('display','block','important');
            el.setAttribute('class','growth-route is-explored');
        });
    }''')
    page.set_viewport_size({'width': 3542, 'height': 2400})
    page.mouse.move(0, 0)
    page.evaluate("Alpine.$data(document.querySelector('.home-growth')).hovered = null")
    svg.screenshot(path='/tmp/tuneconsole-1161-current-open.png', animations='disabled')
    routes.locator('.growth-route').evaluate_all(
        "els => els.forEach(el => el.setAttribute('class','growth-route is-preparing'))")
    svg.screenshot(path='/tmp/tuneconsole-1161-current-preparing.png', animations='disabled')
    assert not errors
