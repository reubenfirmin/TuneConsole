import re
from copy import deepcopy
import logging
from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import expect

from tests.home_fixture_support import browser_store as store, publish_menu, returning_listener
from tests.test_mode_cards_route import prepare_home
from tests.visual_support import capture
from yt_playlist.rec import account_growth, home_cards, home_prepared, home_vibe, mix_tuning, onboarding_outputs
from yt_playlist.util.matching import identity_key


def prepare(store):
    """Finish a private measured-sound input with this app's listening history."""
    # These controls operate after the listener has earned Vibes access.
    returning_listener(store, 'vibes', now=1)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.accounts.get_accounts()[0].id
    for i, genre in enumerate(["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"]):
        tid = store.tracks.upsert_track(f"favorite-{i}", f"Favorite {i}", f"Favorite artist {i}", None, None)
        store.tracks.set_track_genre(tid, genre)
        store.tracks.set_track_year(tid, 1960 + i * 10)
        for day in range(10):
            store.history.record_history_plays(iid, 1 - day * 86400,
                [identity_key(f"Favorite {i}", f"Favorite artist {i}")], source_timezone="UTC")
    prepare_home(store, 1)
    assert store.recommendations.candidate_page('fresh', 'all', 'fresh', limit=1)['tracks']


@pytest.fixture(autouse=True)
def finish_vibe_routes(page):
    yield
    # A root-page route.fetch can still be completing after the last assertion.
    # Drain original handlers before context teardown or the next case inherits
    # their TargetClosedError. Waiting also preserves failures in those handlers.
    page.unroute_all(behavior='wait')


@pytest.fixture
def prepared_vibe_app(request, measured_browser_home_store, home_pages_only):
    # Restore before live_app creates its account, playlist and application
    # settings. A later SQL restore would discard that live app's state.
    base = request.getfixturevalue('live_app')
    return base


def prepare_controls(store):
    """Render a complete board and dense controls without fitting/preparing music."""
    returning_listener(store, 'vibes', now=1)
    payload = publish_menu(store, 1)
    genres = ['jazz', 'techno', 'trance', 'rock-indie', 'ambient', 'classical']
    payload['fingerprint'] = {'breadth': {'families': dict.fromkeys(genres, 1 / 6), 'breadth': .8},
                              'eras': [[str(year), 1 / 6] for year in range(1960, 2020, 10)],
                              'transient': {}}
    payload['genre_options'] = [{'family': genre, 'label': genre} for genre in genres]
    store.rec_proposals.publish_library_taste_summary({**payload['fingerprint'],
        'genre_options': payload['genre_options'], 'genre_plays': {}}, 1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')


def test_opening_mix_controls_checks_vibes_on_demand(live_app, store, page, monkeypatch):
    prepare_controls(store)
    monkeypatch.setattr(onboarding_outputs, 'mixes_ready', lambda *_: True)
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {'episodes': {'mixes': 1}}}, 1)
    store.tv_preparation.record_draw([])
    evaluated = []

    def presets(reader, now):
        evaluated.append(now)
        return True

    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', presets)
    monkeypatch.setattr(type(store.readiness), 'vibe_evidence', lambda _reader, _patterns: (1, 0))
    navigation = []
    background_url = '**/home/growth?navigation=true'
    page.route(background_url, lambda route: navigation.append(route))
    with page.expect_request(background_url):
        page.goto(live_app + '/')
    launcher = page.get_by_role('button', name='Tune these mixes', exact=True)
    expect(launcher).to_be_visible()
    assert not evaluated
    with page.expect_response('**/home/growth') as result:
        launcher.click()
        navigation[0].continue_()
    assert result.value.json()['vibes_ready'] is True
    expect(page.get_by_role('tab', name='Vibe', exact=True)).to_have_attribute('aria-selected', 'true')
    assert evaluated == [1]


@pytest.fixture
def published_vibe_controls(store, monkeypatch, unlocked_home_controls):
    """Exercise post-unlock UI consumers with controlled, already prepared music.

    The persistence/reset and genre-focus journeys below retain real ranking.
    These cases test browser lifecycle, not numerical selection or readiness.
    Routes, settings, cache publication, templates and HTMX still run normally.
    """
    prepare_controls(store)
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    prepared = home_prepared.cards(store, store.rec_proposals.get_proposals('home_prepared'),
                                   1, service, record=False)
    assert len(prepared.protos) == 4 and not prepared.preparing

    def ready_cards(service, request):
        result = deepcopy(prepared)
        result.epoch = home_cards._epoch(service.store, 'cards')
        result.vibe = home_vibe.current(service.store, request.now)
        for proto in result.protos:
            proto['recipe']['vibe'] = result.vibe
        return result

    monkeypatch.setattr(home_cards.HomeCards, 'cards', ready_cards)


@pytest.fixture
def live_mix_tapes(monkeypatch, published_vibe_controls):
    def prepared_menu_used(*args, **kwargs):
        pytest.fail('Live Mix Tapes used a prepared menu')

    for name in ('load', 'peek', 'cards', 'remember', 'prepare_mixes'):
        monkeypatch.setattr(home_prepared, name, prepared_menu_used)


def test_live_home_and_vibe_return_cards_without_polling_or_replacing_an_open_mix(
        live_mix_tapes, live_app, store, page):
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    paths = []
    page.on('request', lambda request: paths.append(request.url.removeprefix(live_app)))
    pending = []
    page.route('**/home/vibe', lambda route: pending.append(route))

    page.goto(live_app)
    board = page.locator('#home-mode-cards')
    expect(board.locator('.gen-card')).to_have_count(4)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    page.get_by_role('radio', name='Relax', exact=True).check()
    feedback = page.locator('#home-vibe-feedback')
    expect(feedback).to_have_attribute('data-state', 'saving')
    card = board.locator('.gen-card').first
    card.locator('.gen-poster').click()
    back = card.get_by_role('button', name='Back to all mixes', exact=True)
    expect(back).to_be_visible()

    with page.expect_response('**/home/vibe') as result:
        pending[0].continue_()
    assert result.value.ok
    assert 'data-vibe="relax"' in result.value.text()
    assert 'x-home-vibe-request' not in result.value.headers
    expect(board).to_have_attribute('data-vibe', '')
    expect(feedback).not_to_be_visible()
    expect(card.get_by_role('button', name='Save and play this mix', exact=True)).to_be_enabled()
    back.click()
    expect(board).to_have_attribute('data-vibe', 'relax')
    expect(feedback).not_to_be_visible()
    assert store.settings.get_setting(home_vibe.SETTING) == 'relax'
    assert store.rec_proposals.vibe_request() is None

    page.reload()
    expect(board).to_have_attribute('data-vibe', 'relax')
    expect(board.locator('.gen-card')).to_have_count(4)
    page.unroute('**/home/vibe')
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    page.get_by_role('radio', name='Take it as it comes', exact=True).check()
    expect(board).to_have_attribute('data-vibe', '')
    expect(feedback).not_to_be_visible()
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert not any(path.startswith(('/home/vibe/status', '/home/cards')) for path in paths)


def test_live_vibe_applies_when_an_unconfirmed_mix_save_ends(live_mix_tapes, live_app, page):
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(live_app)
    board = page.locator('#home-mode-cards')
    old_card = board.locator('.gen-card').first
    old_card.evaluate('card => card.dataset.generationPending = "true"')

    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    with page.expect_response('**/home/vibe') as response:
        page.get_by_role('radio', name='Energize', exact=True).check()
    assert response.value.ok
    feedback = page.locator('#home-vibe-feedback')
    expect(feedback).not_to_be_visible()
    expect(board).to_have_attribute('data-vibe', '')

    old_card.evaluate('''card => generationFailure({node: card, context: generationContext(card)})''')
    expect(board).to_have_attribute('data-vibe', 'energize')
    expect(feedback).not_to_be_visible()
    expect(board.locator('.gen-card')).to_have_count(4)


@pytest.mark.parametrize('outcome', ['success', 'abort'])
def test_live_vibe_banner_is_visible_only_during_the_request(live_mix_tapes, live_app, page, outcome):
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    polls = []
    page.on('request', lambda request: polls.append(request.url) if '/home/vibe/status' in request.url else None)
    requests = []
    page.route('**/home/vibe', lambda route: requests.append(route))
    page.goto(live_app)
    board = page.locator('#home-mode-cards')
    feedback = page.locator('#home-vibe-feedback')
    expect(feedback).not_to_be_visible()

    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    page.get_by_role('radio', name='Lift my mood', exact=True).check()
    expect(feedback).to_be_visible()

    if outcome == 'success':
        with page.expect_response('**/home/vibe') as response:
            requests[0].continue_()
        assert response.value.ok
        expect(board).to_have_attribute('data-vibe', 'uplift')
    else:
        with page.expect_event('requestfailed', lambda request: request.url.endswith('/home/vibe')):
            requests[0].abort()
        expect(board).to_have_attribute('data-vibe', '')
    expect(feedback).not_to_be_visible()
    # Even if a separate handler leaves its visibility flag set, the banner
    # must disappear when HTMX releases the request indicator.
    feedback.evaluate('banner => { banner.hidden = false; }')
    expect(feedback).not_to_be_visible()
    expect(board.locator('.gen-card')).to_have_count(4)
    assert not polls


def readiness_fragment(html, *, preparing, notice='', vibe=None):
    """Change only board readiness attributes, preserving the real rendered cards."""
    class BoardTag(HTMLParser):
        opening = None
        attributes = None

        def handle_starttag(self, tag, attrs):
            if tag == 'div' and dict(attrs).get('id') == 'home-mode-cards':
                self.opening = self.get_starttag_text()
                self.attributes = dict(attrs)

    parser = BoardTag()
    parser.feed(html)
    assert parser.opening
    attrs = parser.attributes
    attrs.update({'data-preparing': str(preparing).lower(), 'data-preference-notice': notice})
    if vibe is not None:
        attrs['data-vibe'] = vibe
    if preparing:
        attrs.update({'hx-get': '/home/cards?prepare_attempt=1', 'hx-trigger': 'every 3s', 'hx-swap': 'outerHTML'})
    else:
        for name in ['hx-get', 'hx-trigger', 'hx-swap']:
            attrs.pop(name, None)
    opening = '<div ' + ' '.join(name if value is None else f'{name}="{escape(value, quote=True)}"'
                                 for name, value in attrs.items()) + '>'
    return html.replace(parser.opening, opening, 1)




def incomplete_board_route(route):
    response = route.fetch()
    route.fulfill(response=response, body=readiness_fragment(response.text(), preparing=True))


def test_vibe_reset_after_reload_does_not_poll_an_old_receipt(live_mix_tapes, live_app, store, page):
    store.rec_proposals.save_vibe_request({'id': 'old-relax', 'vibe': 'relax', 'phase': 'pending'})
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    requests = []
    page.on('request', lambda request: requests.append(request.url.removeprefix(live_app)))
    page.goto(live_app)
    page.reload()
    board = page.locator('#home-mode-cards')
    expect(board.locator('.gen-card')).to_have_count(4)
    assert page.request.post(live_app + '/home/fingerprint/reset').ok
    page.reload()
    expect(board).to_have_attribute('data-vibe', '')
    expect(board.locator('.gen-card')).to_have_count(4)
    expect(page.locator('#home-vibe-feedback')).to_be_hidden()
    assert not any(url.startswith(('/home/vibe/status', '/home/cards?vibe_request')) for url in requests)


@pytest.mark.parametrize('interruption', ['503', 'timeout'])
def test_board_read_recovery_replaces_the_board_once(live_mix_tapes, live_app, page, stalled_routes, interruption):
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.route(live_app + '/', incomplete_board_route)
    requests = []

    def answer(route):
        requests.append(route.request)
        if len(requests) > 1:
            route.continue_()
        elif interruption == '503':
            route.fulfill(status=503)
        else:
            stalled_routes.append(route)

    page.route('**/home/cards?prepare_attempt=1', answer)
    page.add_init_script('''
        window.boardCount = 0;
        new MutationObserver(() => {
          window.boardCount = Math.max(window.boardCount, document.querySelectorAll('#home-mode-cards').length);
        }).observe(document, {subtree: true, childList: true});
        document.addEventListener('htmx:configRequest', event => {
          if (event.detail.path.startsWith('/home/cards?prepare_attempt=')) event.detail.timeout = 750;
        });
    ''')
    page.goto(live_app)
    board = page.locator('#home-mode-cards')
    expect(board).to_have_attribute('data-preparing', 'true')
    expect(board.locator('.gen-card')).to_have_count(4)
    expect(board).to_have_attribute('data-preparing', 'false', timeout=10000)
    expect(board.locator('.gen-card')).to_have_count(4)
    assert len(requests) == 2
    assert page.evaluate('window.boardCount') == 1


def test_latest_vibe_cancels_a_stalled_board_read(live_mix_tapes, live_app, page, stalled_routes):
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.route(live_app + '/', incomplete_board_route)
    background_read = '**/home/cards?prepare_attempt=1'
    page.route(background_read, lambda route: stalled_routes.append(route))
    with page.expect_request(background_read):
        page.goto(live_app)
    board = page.locator('#home-mode-cards')
    expect(board.locator('.gen-card')).to_have_count(4)

    pending_vibe = []
    page.route('**/home/vibe', lambda route: pending_vibe.append(route))
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    try:
        with page.expect_event('requestfailed', lambda request: 'prepare_attempt=1' in request.url, timeout=4000):
            page.get_by_role('radio', name='Energize', exact=True).check()
        expect(board).to_have_attribute('data-vibe', '')
        with page.expect_response('**/home/vibe') as applied:
            pending_vibe.pop().continue_()
    finally:
        stalled_routes.extend(pending_vibe)
    assert applied.value.ok
    expect(board).to_have_attribute('data-vibe', 'energize')
    expect(board.locator('.gen-card')).to_have_count(4)
    expect(page.locator('#home-vibe-feedback')).to_be_hidden()


def test_vibe_applies_persists_recovers_from_failure_and_resets(prepared_vibe_app, store, page):
    live_app = prepared_vibe_app
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app)
    expect(page.locator('#home-mode-cards .gen-card')).to_have_count(4)
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-preparing', 'false')
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_have_attribute("aria-selected", "true")
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    expect(dialog.locator(".fp-vibe-card")).to_have_count(7)
    expect(dialog.get_by_role("radio")).to_have_count(7)
    for name in ['Relax', 'Focus', 'Cruise', 'Energize', 'Lift my mood', 'Let off steam']:
        expect(dialog.get_by_role('radio', name=name, exact=True)).to_have_count(1)
    gentle = dialog.get_by_role("radio", name="Relax", exact=True)
    with page.expect_response("**/home/vibe"):
        gentle.check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "relax")
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback')).not_to_be_visible()
    assert store.settings.get_setting(home_vibe.SETTING) == "relax"
    page.reload()
    expect(dialog).not_to_be_visible()
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    expect(gentle).to_be_checked()
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_have_attribute("aria-selected", "true")
    page.mouse.move(0, 0)
    selected_card = dialog.locator('.fp-vibe-card:has(input:checked) .fp-vibe-content')
    expect(selected_card).to_have_css('opacity', '1')
    expect(selected_card.locator('.fp-vibe-check')).to_have_css('opacity', '1')
    for alternative in dialog.locator('.fp-vibe-card:has(input:not(:checked)) .fp-vibe-content').all():
        expect(alternative).to_have_css('opacity', '0.45')

    before = page.locator('#home-mode-cards .gen-row').evaluate_all('rows => rows.map(row => row.dataset.key)')
    page.route("**/home/vibe", lambda route: route.fulfill(status=500, body="Unavailable"))
    # A rejected save restores Relax before check() can observe Energize checked.
    with page.expect_response("**/home/vibe") as failed_save:
        dialog.get_by_role("radio", name="Energize", exact=True).click()
    assert failed_save.value.status == 500
    assert parse_qs(failed_save.value.request.post_data)['vibe'] == ['energize']
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback')).not_to_be_visible()
    assert page.locator('#home-mode-cards .gen-row').evaluate_all('rows => rows.map(row => row.dataset.key)') == before
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    expect(gentle).to_be_checked()
    expect(dialog.locator(".fp-vibe-fields")).to_be_enabled()
    page.unroute("**/home/vibe")
    with page.expect_response("**/home/vibe"):
        dialog.get_by_role("radio", name="Take it as it comes", exact=True).check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "")
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback')).not_to_be_visible()
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    gentle.check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "relax")
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    # Reset closes and replaces the controls on the successful server response.
    with page.expect_response('**/home/fingerprint/reset') as reset:
        dialog.get_by_role("button", name="Reset mix adjustments", exact=True).click()
    assert reset.value.status == 200
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback')).not_to_be_visible()
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', '')
    assert not store.settings.get_setting(home_vibe.SETTING)
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    expect(dialog.locator('[role="tabpanel"]:visible')).to_have_count(1)
    assert not errors


def test_slow_vibe_request_closes_dialog_and_keeps_ready_mixes_usable(
        live_app, page, published_vibe_controls, stalled_routes):
    viewports = [(1440, 1000), (1920, 1080)]
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.route('**/home/vibe', lambda route: stalled_routes.append(route))
    page.goto(live_app)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    dialog = page.get_by_role('dialog', name='Tune these mixes', exact=True)
    for width, height in viewports:
        page.set_viewport_size({'width': width, 'height': height})
        expect(dialog).to_be_in_viewport()
        capture(page, path=f'build/test-reports/home-discovery-browser/vibe-controls-{width}.png')
    dialog.get_by_role('radio', name='Relax', exact=True).check()
    expect(dialog).not_to_be_visible()
    feedback = page.locator('#home-vibe-feedback')
    expect(feedback).to_have_attribute('data-state', 'saving')
    expect(page.locator('#home-vibe-feedback-title')).to_have_text('Adjusting your mixes for Relax…')
    for width, height in viewports:
        page.set_viewport_size({'width': width, 'height': height})
        expect(feedback).to_be_in_viewport()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    # Exercise a ready card while the vibe POST is still held at the network boundary.
    card = page.locator('#home-mode-cards .gen-card').first
    card.locator('.gen-poster').click()
    expect(card).to_have_class(re.compile(r'\bis-focused\b'))
    expect(card.get_by_role('button', name='Save and play this mix', exact=True)).to_be_enabled()
    expect(card.locator('.gen-track-play').first).to_be_visible()
    expect(feedback).to_have_attribute('data-state', 'saving')
    expect(feedback).to_be_in_viewport()
    expect(page.locator('#home-vibe-feedback')).to_have_count(1)
    assert len(stalled_routes) == 1
    focused = card.element_handle()
    with page.expect_response('**/home/vibe'):
        stalled_routes[0].continue_()
        stalled_routes.pop()
    # Completed preparation must preserve the open mix and its save target.
    assert card.evaluate('(card, previous) => card === previous', focused)
    expect(card).to_have_class(re.compile(r'\bis-focused\b'))
    expect(card.get_by_role('button', name='Save and play this mix', exact=True)).to_be_enabled()
    card.get_by_role('button', name='Back to all mixes', exact=True).click()
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', 'relax')
    expect(feedback).not_to_be_visible()
    for width, height in viewports:
        page.set_viewport_size({'width': width, 'height': height})
        expect(card.locator('.gen-poster')).to_be_visible()
        capture(page, path=f'build/test-reports/home-discovery-browser/home-ready-{width}.png')


def test_vibe_response_excludes_a_dislike_confirmed_before_selection_arrives(
        request, measured_browser_home_store, page, stalled_routes):
    live_app = request.getfixturevalue('live_app')
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(live_app)
    board = page.locator('#home-mode-cards')
    tune = page.get_by_role('button', name='Tune these mixes', exact=True)
    relax = page.get_by_role('radio', name='Relax', exact=True)
    natural = page.get_by_role('radio', name='Take it as it comes', exact=True)

    tune.click()
    relax.check()
    expect(board).to_have_attribute('data-vibe', 'relax')
    track = board.locator('#gen-wheelhouse .gen-row').first
    key, video = track.get_attribute('data-key'), track.get_attribute('data-vid')
    assert key and video
    tune.click()
    natural.check()
    expect(board).to_have_attribute('data-vibe', '')

    pending = []
    page.route('**/home/vibe', lambda route: pending.append(route))
    try:
        tune.click()
        relax.check()
        expect(board).to_have_attribute('data-vibe', '')
        expect(board.locator('.gen-card')).to_have_count(4)
        rating = page.request.post(live_app + '/now-playing/rate',
            data={'videoId': video, 'action': 'dislike', 'likeStatus': 'DISLIKE'})
        assert rating.ok and rating.json()['ok']
        assert len(pending) == 1
        with page.expect_response('**/home/vibe') as applied:
            pending.pop().continue_()
    finally:
        stalled_routes.extend(pending)
    assert applied.value.ok
    expect(board).to_have_attribute('data-vibe', 'relax')
    expect(board.locator('.gen-card')).to_have_count(4)
    assert key not in board.locator('.gen-row').evaluate_all('rows => rows.map(row => row.dataset.key)')














def test_vibe_respects_genre_focus_through_clear_and_expiry(prepared_vibe_app, store, page):
    live_app = prepared_vibe_app
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.clock.install()
    page.goto(live_app)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    dialog = page.get_by_role('dialog', name='Tune these mixes', exact=True)
    dialog.get_by_role('tab', name='Genre focus', exact=True).click()
    dialog.get_by_role('checkbox', name='jazz', exact=True).check()
    with page.expect_response('**/home/mix-genres'):
        dialog.get_by_role('button', name='Make mixes', exact=True).click()
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    dialog.get_by_role('tab', name='Vibe', exact=True).click()
    dialog.get_by_role('radio', name='Relax', exact=True).check()
    row = page.locator('#home-mode-cards')
    expect(row).to_have_attribute('data-vibe', 'relax')
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']
    assert row.locator('[data-genres]').evaluate_all(
        'els => els.length > 0 && els.every(el => JSON.parse(el.dataset.genres).every(g => g.toLowerCase() === "jazz"))')
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    dialog.get_by_role('tab', name='Genre focus', exact=True).click()
    expect(dialog.get_by_role('checkbox', name='jazz', exact=True)).to_be_checked()
    expect(dialog.locator('#home-genre-count')).to_have_text('1 genre in the mix.')
    dialog.get_by_role('tab', name='Vibe', exact=True).click()
    with page.expect_response('**/home/vibe') as cleared:
        dialog.get_by_role('radio', name='Take it as it comes', exact=True).check()
    assert cleared.value.ok
    expect(row).to_have_attribute('data-vibe', '')
    assert store.settings.get_setting(home_vibe.SETTING) is None
    expect(row.locator('.gen-card')).to_have_count(4)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    expect(dialog.get_by_role('radio', name='Take it as it comes', exact=True)).to_be_checked()
    expect(page.locator('.fp-tuning-dot')).to_be_visible()  # The independent Jazz focus remains active.
    vibe_dot = page.locator('#fp-vibe-tab .fp-tab-dot')
    expect(vibe_dot).to_have_js_property('hidden', True)
    expect(vibe_dot).not_to_be_visible()
    assert row.locator('[data-genres]').evaluate_all(
        'els => els.length > 0 && els.every(el => JSON.parse(el.dataset.genres).every(g => g.toLowerCase() === "jazz"))')
    with page.expect_response('**/home/vibe') as selected:
        dialog.get_by_role('radio', name='Relax', exact=True).check()
    assert selected.value.ok
    expect(row).to_have_attribute('data-vibe', 'relax')
    store.rec_model.set_lean('genre:techno', 1.6, 1)
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response('**/home/fingerprint'):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(row).to_have_attribute('data-vibe', '')
    assert store.rec_model.get_lean('genre:techno') == 1.6
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']
