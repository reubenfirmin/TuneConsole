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
from yt_playlist.rec import home_cards, home_prepared, home_vibe, mix_tuning
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
    store.rec_proposals.put_proposals('home_prepared', payload, 1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')


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
    expect(page.locator('#home-vibe-feedback')).to_have_attribute('data-state', 'ready')
    expect(page.locator('#home-vibe-feedback-title')).to_have_text('Your Relax mix tapes are ready.')
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
    expect(page.locator('#home-vibe-feedback')).to_have_attribute('data-state', 'error')
    expect(page.locator('#home-vibe-feedback-title')).to_have_text('Couldn’t update your mix tapes.')
    assert page.locator('#home-mode-cards .gen-row').evaluate_all('rows => rows.map(row => row.dataset.key)') == before
    page.get_by_role('button', name='Change vibe', exact=True).click()
    expect(gentle).to_be_checked()
    expect(dialog.locator(".fp-vibe-fields")).to_be_enabled()
    page.unroute("**/home/vibe")
    with page.expect_response("**/home/vibe"):
        dialog.get_by_role("radio", name="Take it as it comes", exact=True).check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "")
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback-title')).to_have_text('Your mix tapes are ready.')
    page.get_by_role('button', name='Change vibe', exact=True).click()
    gentle.check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "relax")
    page.get_by_role('button', name='Change vibe', exact=True).click()
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


@pytest.mark.parametrize('width,height', [(1440, 1000)])
def test_slow_vibe_request_closes_dialog_and_keeps_ready_mixes_usable(
        live_app, store, page, width, height, published_vibe_controls):
    page.set_viewport_size({'width': width, 'height': height})
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    pending = []
    page.route('**/home/vibe', lambda route: pending.append(route))
    page.goto(live_app)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    dialog = page.get_by_role('dialog', name='Tune these mixes', exact=True)
    dialog.get_by_role('radio', name='Relax', exact=True).check()
    expect(dialog).not_to_be_visible()
    feedback = page.locator('#home-vibe-feedback')
    expect(feedback).to_be_in_viewport()
    expect(feedback).to_have_attribute('data-state', 'saving')
    expect(page.locator('#home-vibe-feedback-title')).to_have_text('Adjusting your mixes for Relax…')
    expect(page.locator('#home-vibe-feedback-detail')).to_be_empty()
    expect(page.locator('#home-vibe-feedback-detail')).not_to_be_visible()
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
    assert len(pending) == 1
    focused = card.element_handle()
    with page.expect_response(lambda response: response.url.endswith('/home/vibe/status')
                              and response.json().get('phase') == 'ready'):
        pending[0].continue_()
    # Completed preparation must preserve the open mix and its save target.
    assert card.evaluate('(card, previous) => card === previous', focused)
    expect(card).to_have_class(re.compile(r'\bis-focused\b'))
    expect(card.get_by_role('button', name='Save and play this mix', exact=True)).to_be_enabled()
    card.get_by_role('button', name='Back to all mixes', exact=True).click()
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', 'relax')
    expect(feedback).to_have_attribute('data-state', 'ready')














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
    page.get_by_role('button', name='Change vibe', exact=True).click()
    dialog.get_by_role('tab', name='Genre focus', exact=True).click()
    expect(dialog.get_by_role('checkbox', name='jazz', exact=True)).to_be_checked()
    expect(dialog.locator('#home-genre-count')).to_have_text('1 genre in the mix.')
    dialog.get_by_role('tab', name='Vibe', exact=True).click()
    previous_board = row.element_handle()
    with page.expect_response(lambda response: response.url.endswith('/home/vibe/status')
                              and response.json().get('vibe') == ''
                              and response.json().get('phase') == 'waiting'):
        dialog.get_by_role('radio', name='Take it as it comes', exact=True).check()
    assert store.settings.get_setting(home_vibe.SETTING) is None
    # This measured input has only two ordinary Jazz lanes. Clearing is saved,
    # while the existing complete Jazz board remains usable until more are ready.
    assert row.evaluate('(row, previous) => row === previous', previous_board)
    expect(row.locator('.gen-card')).to_have_count(4)
    expect(page.locator('#home-vibe-feedback')).to_have_attribute('data-state', 'waiting')
    page.get_by_role('button', name='Change vibe', exact=True).click()
    expect(dialog.get_by_role('radio', name='Take it as it comes', exact=True)).to_be_checked()
    expect(page.locator('.fp-tuning-dot')).to_be_visible()  # The independent Jazz focus remains active.
    vibe_dot = page.locator('#fp-vibe-tab .fp-tab-dot')
    expect(vibe_dot).to_have_js_property('hidden', True)
    expect(vibe_dot).not_to_be_visible()
    assert row.locator('[data-genres]').evaluate_all(
        'els => els.length > 0 && els.every(el => JSON.parse(el.dataset.genres).every(g => g.toLowerCase() === "jazz"))')
    with page.expect_response('**/home/vibe') as selected:
        dialog.get_by_role('radio', name='Relax', exact=True).check()
    expect(row).to_have_attribute('data-vibe-request', selected.value.headers['x-home-vibe-request'])
    expect(row).to_have_attribute('data-vibe', 'relax')
    store.rec_model.set_lean('genre:techno', 1.6, 1)
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response('**/home/fingerprint'):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(row).to_have_attribute('data-vibe', '')
    assert store.rec_model.get_lean('genre:techno') == 1.6
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']
