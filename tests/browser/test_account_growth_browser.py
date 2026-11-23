"""Rendered growth states and notification actions."""
from tests.enrichment_support import complete_entries, complete_discovered
import json
import re

import pytest
from playwright.sync_api import expect

from tests.test_account_growth import catalog, ready_radio
from tests.onboarding_support import publish_live_mix_inputs, publish_personal_album, publish_personal_mix
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
    if request.node.originalname in ('test_festival_states_notices_home_choice_and_bonus_details',
                                     'test_home_keeps_earlier_tabs_visible_while_checking_later_tabs'):
        # This interaction-only journey repeatedly serves the same prepared
        # state. Keep ordinary private WAL reads instead of memory snapshots.
        return request.getfixturevalue('file_backed_store')
    return store


def test_direct_recent_page_waits_for_ordinary_mixes(live_app, store, page, monkeypatch):
    from tests.test_growth_destinations import publish_destinations
    from tests.test_growth_readiness import publish_vibe_menus
    from yt_playlist.rec import tv

    store.settings.set_setting('last_sync_at', '1')
    publish_vibe_menus(store, genre_reserves=False)
    publish_destinations(store, now=1)
    store.tv_preparation.record_draw([entry['video_id'] for entry in tv.queue(store, 1)])
    mixes_ready = False
    monkeypatch.setattr(account_growth.onboarding_outputs, 'mixes_ready', lambda *_: mixes_ready)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.route('https://**', lambda route: route.abort())
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))

    page.goto(live_app + '/?panel=unlocks#unlocks')
    landmark = page.get_by_role('button', name=re.compile('^Into recently,'))
    expect(landmark).to_have_class(re.compile(r'\bis-untouched\b'))
    expect(page.get_by_role('tab', name='Into recently', exact=True)).to_be_hidden()

    page.goto(live_app + '/?panel=recent#recent')
    stories = page.get_by_role('region', name='Into recently')
    expect(stories).to_be_hidden()
    mixes_ready = True
    store.settings.set_setting('last_sync_at', '2')  # Invalidate the cached growth projection.
    page.goto(live_app + '/?panel=recent#recent')
    expect(stories).to_be_visible()
    page.screenshot(path='/tmp/tuneconsole-1489-recent.png')


@pytest.mark.parametrize('entry', ['direct', 'tab'])
def test_into_recently_loads_magazine_styles_and_records_visible_stories(live_app, store, page, entry):
    from tests.test_growth_destinations import publish_destinations
    from tests.test_growth_readiness import publish_vibe_menus
    from yt_playlist.rec import into_recently, tv

    returning_listener(store, 'vibes')
    publish_vibe_menus(store, genre_reserves=False)
    publish_destinations(store, now=1)
    publish_personal_mix(store, now=1)
    store.tv_preparation.record_draw([item['video_id'] for item in tv.queue(store, 1)])
    store.settings.set_setting('last_sync_at', '1')
    prepared = store.rec_proposals.get_proposals(into_recently.SURFACE)
    slowdive, shoegaze, biosphere = prepared['cards']
    prepared['cards'] = [
        {**slowdive, 'subject': 'artist:Slowdive', 'display': 'Slowdive', 'title': 'Slowdive',
         'thumbnail': '/static/vibes/relax.webp', 'extract': 'Layered guitars drift through soft melodies.'},
        {**shoegaze, 'subject': 'genre:Shoegaze', 'display': 'Shoegaze', 'title': 'Shoegaze',
         'kind': 'genre', 'group': 'artist:Slowdive', 'thumbnail': None,
         'extract': 'Guitar textures soften the boundaries between chords.'},
        {**biosphere, 'subject': 'artist:Biosphere', 'display': 'Biosphere', 'title': 'Biosphere',
         'thumbnail': '/static/vibes/concentrate.webp', 'extract': 'Ambient recordings make room for silence.'},
        {**biosphere, 'subject': 'artist:Lusine', 'display': 'Lusine', 'title': 'Lusine',
         'thumbnail': '/static/vibes/natural.webp', 'extract': 'Small electronic rhythms carry the melody.'},
    ]
    store.rec_proposals.put_proposals(into_recently.SURFACE, prepared, 1)
    before = store.rec_ledger.story_views()
    prefetched = page.request.get(live_app + '/?panel=recent&fragment=1')
    assert prefetched.ok and 'data-story-subject' in prefetched.text()
    assert store.rec_ledger.story_views() == before
    page.set_viewport_size({'width': 1440, 'height': 1440})
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))

    arrivals = []
    page.on('response', lambda response: arrivals.append('styles')
            if response.url.split('?')[0].endswith('/static/home-discovery.css') else None)
    page.on('request', lambda request: arrivals.append('stories')
            if request.url.endswith('/?panel=recent&fragment=1') else None)
    if entry == 'tab':
        page.goto(live_app + '/?panel=directions#directions')
        assert not arrivals
        assert store.rec_ledger.story_views() == before

    subject = 'artist:Slowdive'
    companion = 'genre:Shoegaze'
    with page.expect_response(lambda response: response.url.endswith('/home/into-recently/impressions')
                              and {subject, companion}.issubset(response.request.post_data_json['subjects'])) as impression:
        if entry == 'tab':
            page.get_by_role('tab', name='Into recently', exact=True).click()
        else:
            page.goto(live_app + '/?panel=recent#recent')
        stories = page.get_by_role('region', name='Into recently')
        expect(stories.locator('.into-card')).to_have_count(4)
        lead = stories.locator('.into-feature').first
        heading = lead.locator(f'[data-story-subject="{subject}"]')
        heading.evaluate("heading => heading.scrollIntoView({block: 'center', behavior: 'instant'})")
        expect(heading).to_be_in_viewport(ratio=0.5)
        expect(stories.locator(f'[data-story-subject="{companion}"]')).to_be_in_viewport(ratio=0.5)
    assert impression.value.status == 204
    views = store.rec_ledger.story_views()
    for seen in (subject, companion):
        assert views[seen]['views'] == before.get(seen, {}).get('views', 0) + 1
    lead.locator('.into-art img').evaluate('image => image.decode()')
    assert arrivals == (['styles', 'stories'] if entry == 'tab' else ['styles'])
    expect(stories.locator('.into-spread').first).to_have_css('display', 'grid')
    expect(lead.locator('.into-subject')).to_have_css('font-family', re.compile(r'Georgia, serif$'))
    expect(lead.locator('.into-art img')).to_have_css('object-fit', 'cover')
    expect(lead.locator('.into-songs')).to_have_css('display', 'grid')
    expect(stories.locator('.into-companions .into-text-only')).to_be_visible()
    expect(page.locator('head link[href*="/static/radio.css"]')).to_have_count(0)


def test_home_keeps_earlier_tabs_visible_while_checking_later_tabs(
        live_app, store, page, monkeypatch, stalled_routes):
    from tests.test_growth_destinations import finish_full_journey

    returning_listener(store, 'mixes')
    store.settings.set_setting('home_format', 'mixes')
    store.settings.set_setting('last_sync_at', '1')
    publish_personal_album(store, now=1, count=2)
    publish_personal_mix(store, now=1)
    publish_live_mix_inputs(store, now=1)
    finish_full_journey(store, now=1)
    monkeypatch.setattr(account_growth.growth_readiness, 'foundation', lambda *args: True)
    vibe_checks = []

    def vibe_evidence(*args):
        vibe_checks.append(True)
        return True

    monkeypatch.setattr(type(store.readiness), 'has_vibe_evidence', vibe_evidence)
    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', lambda *args: True)
    destination_requests = []
    background_url = '**/home/growth?navigation=true'
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.route(background_url, lambda route: stalled_routes.append(route), times=1)
    page.on('request', lambda request: destination_requests.append(request.url)
            if 'fragment=1' in request.url else None)
    with page.expect_request(background_url):
        page.goto(live_app + '/', wait_until='domcontentloaded')
    expect(page.get_by_role('tab', name='Mix tapes')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#home-mode-cards > .gen-card')).to_have_count(4)
    expect(page.locator('#home-mode-cards .gen-play')).to_have_count(4)
    expect(page.get_by_role('tab', name='Long play')).to_be_visible()
    expect(page.get_by_role('tab', name='Discover')).to_be_visible()
    expect(page.get_by_role('tab', name='On air')).to_be_hidden()
    assert not vibe_checks and not destination_requests

    with page.expect_response(background_url) as result:
        stalled_routes[0].continue_()
        stalled_routes.pop(0)
    assert result.value.ok
    for tab in ('Long play', 'Discover', 'On air', 'TV', 'Revisit', 'Into recently'):
        expect(page.get_by_role('tab', name=tab, exact=True)).to_be_visible()
    assert vibe_checks == [True] and not destination_requests

    with page.expect_response('**/?panel=album-choices&fragment=1') as result:
        page.get_by_role('tab', name='Long play').click()
    assert result.value.ok
    expect(page.get_by_role('tab', name='Long play')).to_have_attribute('aria-selected', 'true')
    expect(page.get_by_role('heading', name='Your next record')).to_be_visible()
    assert len(destination_requests) == 1


def test_integration_notices_and_highlighted_setup_destinations(live_app, store, page, monkeypatch):
    from tests.conftest import seed_catalog_track

    account = store.accounts.get_accounts()[0].id
    seed_catalog_track(store, 'song|artist', 'Song', 'Artist', video_id='song')
    returning_listener(store, 'mixes')
    first = 120
    store.history.record_play_event(account, 'song|artist', 'song', first)
    clock = {'now': first}
    actual_view = account_growth.view
    monkeypatch.setattr(account_growth, 'view', lambda source, _now, **kwargs:
                        actual_view(source, clock['now'], **kwargs))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.add_init_script('''if (!localStorage.getItem('tc-growth-notices-v1')) localStorage.setItem('tc-growth-notices-v1', JSON.stringify({
        seen: ['foundation', 'albums', 'discover', 'listening', 'radio', 'tv', 'vibes', 'recent', 'rediscover'],
        later: true,
    }))''')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app + '/#unlocks', wait_until='domcontentloaded')
    model = "Alpine.$data(document.querySelector('.home-growth'))"
    notice = page.locator('.growth-unlock-notice')
    expect(notice).to_be_hidden()

    clock['now'] = first + 86400
    page.evaluate(f'{model}.refresh()')
    expect(notice).to_contain_text('Last.fm')
    expect(page.get_by_role('tab', name='Your unlocks')).to_have_class(re.compile('has-new'))
    page.screenshot(path='/tmp/tuneconsole-1118-lastfm-notice.png', full_page=True)
    notice.get_by_role('button', name='See what’s new', exact=True).click()
    popup = page.get_by_role('dialog', name='Find more music that fits')
    expect(popup).to_contain_text('Optional bonus')
    popup.get_by_role('link', name='Set up Last.fm', exact=True).click()
    expect(page.get_by_role('tab', name='Enrichment', exact=True)).to_have_attribute('aria-selected', 'true')
    lastfm = page.locator('#setup-lastfm')
    expect(lastfm).to_be_focused()
    expect(lastfm.get_by_text('Last.fm', exact=True)).to_be_in_viewport()
    expect(lastfm).to_have_css('outline-style', 'solid')
    expect(lastfm).to_have_css('outline-width', '3px')
    page.screenshot(path='/tmp/tuneconsole-1118-lastfm-setup.png', full_page=True)

    # Both overdue recommendations queue in the requested order for a returning listener.
    clock['now'] = first + 2 * 86400
    page.goto(live_app + '/#unlocks', wait_until='domcontentloaded')
    expect(notice).to_contain_text('Last.fm')
    notice.get_by_role('button', name='Mark as seen', exact=True).click()
    expect(notice).to_contain_text('Google Takeout')
    notice.get_by_role('button', name='See what’s new', exact=True).click()
    popup = page.get_by_role('dialog', name='Rediscover old favorites sooner')
    popup.get_by_role('link', name='Add past listening', exact=True).click()
    expect(page.get_by_role('tab', name='Import', exact=True)).to_have_attribute('aria-selected', 'true')
    takeout = page.locator('#setup-takeout')
    expect(takeout).to_be_focused()
    expect(takeout.get_by_role('heading', name='Import YouTube history (Google Takeout)')).to_be_in_viewport()
    expect(takeout).to_have_css('outline-width', '3px')
    page.screenshot(path='/tmp/tuneconsole-1118-takeout-setup.png', full_page=True)

    page.goto(live_app + '/#unlocks', wait_until='domcontentloaded')
    expect(notice).to_contain_text('Google Takeout')
    notice.get_by_role('button', name='Mark as seen', exact=True).click()
    expect(notice).to_be_hidden()
    page.reload(wait_until='domcontentloaded')
    expect(notice).to_be_hidden()
    assert not errors


def test_integration_reminders_in_needs_attention(store, page, monkeypatch, request):
    from tests.conftest import seed_catalog_track
    from yt_playlist.web import app as webapp

    first = 120
    clock = {'now': first + 86400}
    create_app = webapp.create_app

    def timed_app(*args, **kwargs):
        return create_app(*args, **{**kwargs, 'now_fn': lambda: clock['now']})

    monkeypatch.setattr(webapp, 'create_app', timed_app)
    monkeypatch.setattr(account_growth.lastfm, 'api_key',
                        lambda source: source.settings.get_setting('lastfm_api_key') or None)
    base = request.getfixturevalue('live_app')
    account = store.accounts.get_accounts()[0].id
    seed_catalog_track(store, 'song|artist', 'Song', 'Artist', video_id='song')
    store.history.record_play_event(account, 'song|artist', 'song', first)
    returning_listener(store, 'mixes')
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    # Acknowledging the unlock notices must not dismiss the later reminders.
    page.add_init_script('''localStorage.setItem('tc-growth-notices-v1', JSON.stringify({
        seen: ['foundation', 'albums', 'discover', 'listening', 'radio', 'tv', 'vibes', 'recent', 'rediscover', 'lastfm', 'takeout'],
        later: true,
    }))''')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    lastfm, takeout = page.locator('#lastfm-nudge'), page.locator('#takeout-nag')
    page.goto(base + '/#unlocks', wait_until='domcontentloaded')
    expect(lastfm).to_have_count(0)
    expect(takeout).to_have_count(0)

    clock['now'] = first + 8 * 86400 - 1
    page.reload(wait_until='domcontentloaded')
    expect(lastfm).to_have_count(0)
    clock['now'] += 1
    page.reload(wait_until='domcontentloaded')
    page.get_by_role('tab', name='Needs attention').click()
    expect(lastfm).to_be_visible()
    expect(takeout).to_have_count(0)
    lastfm.get_by_role('link', name='Add key', exact=False).click()
    expect(page.locator('#setup-lastfm')).to_be_focused()
    expect(page.locator('#setup-lastfm')).to_have_css('outline-width', '3px')

    clock['now'] = first + 9 * 86400
    page.goto(base + '/#notices', wait_until='domcontentloaded')
    expect(page.get_by_role('tab', name='Needs attention')).to_have_attribute('aria-selected', 'true')
    expect(lastfm).to_be_visible()
    expect(takeout).to_be_visible()
    page.screenshot(path='/tmp/tuneconsole-1118-needs-attention.png', full_page=True)
    takeout.get_by_role('link', name='Add listening history', exact=False).click()
    expect(page.locator('#setup-takeout')).to_be_focused()
    expect(page.locator('#setup-takeout')).to_have_css('outline-width', '3px')

    page.goto(base + '/#notices', wait_until='domcontentloaded')
    lastfm.get_by_role('button', name='Dismiss', exact=True).click()
    expect(lastfm).to_have_count(0)
    expect(takeout).to_be_visible()
    page.reload(wait_until='domcontentloaded')
    expect(lastfm).to_have_count(0)
    expect(takeout).to_be_visible()
    takeout.get_by_role('button', name='Dismiss', exact=True).click()
    expect(takeout).to_have_count(0)
    page.reload(wait_until='domcontentloaded')
    expect(lastfm).to_have_count(0)
    expect(takeout).to_have_count(0)

    clock['now'] += 30 * 86400
    page.reload(wait_until='domcontentloaded')
    page.get_by_role('tab', name='Needs attention').click()
    expect(lastfm).to_be_visible()
    expect(takeout).to_have_count(0)
    clock['now'] += 60 * 86400
    page.reload(wait_until='domcontentloaded')
    expect(lastfm).to_be_visible()
    expect(takeout).to_be_visible()
    store.settings.set_setting('lastfm_api_key', 'controlled-key')
    store.history.import_play_events(account, [('song|artist', 'song', 60)], source='takeout')
    page.reload(wait_until='domcontentloaded')
    expect(lastfm).to_have_count(0)
    expect(takeout).to_have_count(0)
    assert not errors


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
    def refresh_growth():
        # Drain any response from before the fixture change, then use the real
        # polling path and wait for Alpine to apply the resulting snapshot.
        page.wait_for_function(f'!{growth_model}.refreshing')
        with page.expect_response('**/home/growth') as refreshed:
            page.evaluate(f'async () => {{ await {growth_model}.refresh(); }}')
        page.wait_for_function(f'!{growth_model}.refreshing')
        return refreshed.value.json()

    # Earn access in the server's private fixture, so polling cannot replace a
    # browser-only override with the still-locked authoritative state.
    original_draw = store.tv_preparation.draw()
    store.tv_preparation.record_draw([])
    server_tv = next(node for node in refresh_growth()['nodes'] if node['id'] == 'tv')
    browser_tv = page.evaluate(f'{growth_model}.node("tv")')
    record_testsuite_property('tv-after-refresh', json.dumps({
        'server': server_tv, 'browser': browser_tv,
    }))
    assert browser_tv == server_tv
    assert server_tv['unlocked'] and server_tv['used'] and server_tv['href'] == '/#tv'
    assert not server_tv['ready'] and not server_tv['usable'] and server_tv['state'] == 'preparing'
    page.get_by_role('button', name=re.compile('^TV,')).click()
    expect(popup.get_by_role('region', name='How to get here')).to_be_hidden()
    expect(popup.get_by_role('link', name='Open TV', exact=True)).to_be_visible()
    page.get_by_role('button', name='Close destination', exact=True).click()
    with store.conn:
        from yt_playlist.core.recommendation_work import TV_DRAW_KEY
        from yt_playlist.core.worker_settings import save

        draw, recent, last = original_draw
        save(store.conn, TV_DRAW_KEY, json.dumps({'number': draw, 'recent': recent, 'last': last}))
    refresh_growth()
    assert not page.evaluate(f'{growth_model}.node("tv").unlocked')
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
