import re
from html import escape
from html.parser import HTMLParser

import pytest
from playwright.sync_api import expect

from tests.test_home_vibe import seed_sounds
from tests.test_mode_cards_route import prepare_home
from yt_playlist.rec import home_vibe, mix_tuning
from yt_playlist.util.matching import identity_key


def prepare(store):
    seed_sounds(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.identities.get_identities()[0].id
    for i, genre in enumerate(["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"]):
        tid = store.tracks.upsert_track(f"favorite-{i}", f"Favorite {i}", f"Favorite artist {i}", None, None)
        store.tracks.set_track_genre(tid, genre)
        store.tracks.set_track_year(tid, 1960 + i * 10)
        for day in range(10):
            store.history.record_history_plays(iid, 1 - day * 86400,
                [identity_key(f"Favorite {i}", f"Favorite artist {i}")], source_timezone="UTC")
    prepare_home(store, 1)


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


@pytest.mark.parametrize('activation', ['pointer', 'keyboard'])
def test_default_vibe_closes_without_dimming_or_marking_an_adjustment(live_app, store, page, activation):
    prepare(store)
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append((request.method, request.url)))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(live_app)
    launcher = page.get_by_role('button', name='Tune these mixes', exact=True)
    dot = launcher.locator('.fp-tuning-dot')
    dialog = page.get_by_role('dialog', name='Tune these mixes', exact=True)
    default = dialog.get_by_role('radio', name='Take it as it comes', exact=True)

    def choose_default():
        if activation == 'pointer':
            default.click()
        else:
            default.press('Space')

    def assert_neutral_choices():
        expect(default).to_be_checked()
        expect(dialog.locator('[role="tabpanel"]:visible')).to_have_count(1)
        expect(dialog.locator('#fp-vibe-panel')).to_be_visible()
        expect(dialog.locator('#fp-preferences-panel')).not_to_be_visible()
        expect(dialog.locator('.fp-tab-dot')).to_have_js_property('hidden', True)
        expect(dialog.locator('.fp-tab-dot')).not_to_be_visible()
        for card in dialog.locator('.fp-vibe-content').all():
            expect(card).to_have_css('opacity', '1')
            expect(card).to_have_css('filter', 'none')

    expect(dot).not_to_be_visible()
    launcher.click()
    assert_neutral_choices()
    # Activating an already-selected default still closes, without a redundant POST.
    choose_default()
    expect(dialog).not_to_be_visible()
    assert not [url for method, url in requests if method == 'POST' and url.endswith('/home/vibe')]
    launcher.click()
    with page.expect_response('**/home/vibe'):
        dialog.get_by_role('radio', name='Relax', exact=True).check()
    expect(dialog).not_to_be_visible()
    expect(dot).to_be_visible()
    launcher.click()
    with page.expect_response('**/home/vibe'):
        choose_default()
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', '')
    expect(dot).to_have_js_property('hidden', True)
    launcher.click()
    assert_neutral_choices()
    dialog.get_by_role('button', name='Close mix controls', exact=True).click()
    page.reload()
    expect(dialog).not_to_be_visible()
    expect(dot).not_to_be_visible()
    launcher.click()
    assert_neutral_choices()
    choose_default()
    expect(dialog).not_to_be_visible()
    assert len([url for method, url in requests if method == 'POST' and url.endswith('/home/vibe')]) == 2
    # Clearing the only adjustment must not request an expiry replacement of the controls.
    assert not [url for method, url in requests if '/home/fingerprint' in url]
    assert not errors


def test_vibe_applies_persists_recovers_from_failure_and_resets(live_app, store, page):
    prepare(store)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app)
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
    dialog.get_by_role("radio", name="Energize", exact=True).check()
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
    dialog.get_by_role("button", name="Reset mix adjustments", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback')).not_to_be_visible()
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    expect(dialog.locator('[role="tabpanel"]:visible')).to_have_count(1)
    assert not errors


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 850), (320, 640)])
def test_slow_vibe_request_closes_dialog_and_keeps_ready_mixes_usable(live_app, store, page, width, height):
    prepare(store)
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
    pending[0].continue_()
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', 'relax')
    expect(feedback).to_have_attribute('data-state', 'ready')


def test_vibe_feedback_replacement_keeps_one_owner_and_drops_the_previous_timer(live_app, store, page):
    prepare(store)
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.clock.install()
    page.set_viewport_size({'width': 390, 'height': 850})
    page.goto(live_app)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    page.get_by_role('radio', name='Relax', exact=True).check()
    feedback = page.locator('#home-vibe-feedback')
    expect(feedback).to_have_attribute('data-state', 'ready')
    # Exercise the real feed endpoint and normal HTMX/Alpine cleanup, in one document.
    page.evaluate("""async () => {
      await htmx.ajax('GET', '/home/feed', {target: '#home-feed', swap: 'outerHTML'});
      await Alpine.nextTick();
    }""")
    expect(feedback).to_have_count(1)
    expect(feedback).not_to_be_visible()
    pending = []
    page.route('**/home/vibe', lambda route: pending.append(route))
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    page.get_by_role('radio', name='Energize', exact=True).check()
    expect(feedback).to_have_attribute('data-state', 'saving')
    expect(feedback).to_be_in_viewport()
    page.clock.fast_forward(9000)
    expect(feedback).to_be_in_viewport()
    expect(feedback).to_have_attribute('data-state', 'saving')
    expect(feedback).to_have_count(1)
    assert len(pending) == 1
    pending[0].continue_()
    expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', 'energize')
    page.goto(live_app + '/artists')
    expect(feedback).to_have_count(0)


@pytest.mark.parametrize('notice', ['', "Heads up - we're unable to create relax mix tapes right now."])
def test_vibe_feedback_waits_for_preparation_and_recovers_after_reload(live_app, store, page, notice):
    """Controlled readiness fragments isolate the browser's lifecycle from worker timing."""
    prepare(store)
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.clock.install()
    fragments = {}

    def preparing_response(route):
        response = route.fetch()
        fragments['ready'] = readiness_fragment(response.text(), preparing=False)
        fragments['pending'] = readiness_fragment(response.text(), preparing=True, notice=notice)
        route.fulfill(response=response, body=fragments['pending'])

    page.route('**/home/vibe', preparing_response)
    page.goto(live_app)
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    page.get_by_role('radio', name='Relax', exact=True).check()
    feedback = page.locator('#home-vibe-feedback')
    phase = 'notice' if notice else 'waiting'
    expect(feedback).to_have_attribute('data-state', phase)
    if notice:
        expect(page.locator('#home-vibe-feedback-title')).to_have_text(notice)
    page.route('**/home/cards?prepare_attempt=*', lambda route: route.fulfill(
        status=200, content_type='text/html', body=fragments['pending']))
    with page.expect_response('**/home/cards?prepare_attempt=*'):
        page.clock.fast_forward(15000)
    expect(feedback).to_be_visible()
    expect(feedback).to_have_attribute('data-state', phase)
    page.unroute('**/home/cards?prepare_attempt=*')
    # Reload's initial HTML also reports preparation; no old JS state is available.
    def reload_pending(route):
        response = route.fetch()
        route.fulfill(response=response, body=readiness_fragment(
            response.text(), preparing=True, notice=notice, vibe='relax'))

    page.route(live_app + '/', reload_pending)
    page.reload()
    expect(feedback).to_be_visible()
    expect(feedback).to_have_attribute('data-state', phase)
    page.route('**/home/cards?prepare_attempt=*', lambda route: route.fulfill(
        status=200, content_type='text/html', body=fragments['ready']))
    with page.expect_response('**/home/cards?prepare_attempt=*'):
        page.clock.fast_forward(3000)
    expect(feedback).to_have_attribute('data-state', 'ready')
    expect(page.locator('#home-vibe-feedback-title')).to_have_text('Your Relax mix tapes are ready.')
    page.clock.fast_forward(8000)
    expect(feedback).not_to_be_visible()


@pytest.mark.parametrize('width,height', [(1440, 1000), (1024, 600), (390, 850), (320, 640)])
def test_tuning_launcher_stays_at_mix_board_center_with_preference_notices(live_app, store, page, width, height):
    prepare(store)
    page.set_viewport_size({'width': width, 'height': height})
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(live_app)
    board = page.locator('#home-mode-cards')
    expect(board.locator(':scope > .gen-card')).to_have_count(4)
    # Only the notice text/state is controlled; the board and layout use actual rendered markup.
    for message in ['', "Heads up - we're unable to create relax mix tapes right now.",
                    ' '.join(["Heads up - we're unable to create relax + techno mix tapes right now."] * 4), '']:
        page.locator('#home-mix-status').evaluate('(el, text) => { el.textContent = text; el.hidden = !text; }', message)
        # Sample both rectangles in one frame while the page's normal motion continues.
        centers = board.evaluate("""el => {
          const board = el.getBoundingClientRect();
          const button = document.querySelector('.fp-collapse').getBoundingClientRect();
          return {dx: button.x + button.width / 2 - board.x - board.width / 2,
                  dy: button.y + button.height / 2 - board.y - board.height / 2};
        }""")
        assert abs(centers['dx']) < 2, centers
        assert abs(centers['dy']) < 2, centers


def test_mix_tabs_fit_desktop_mobile_and_short_windows(live_app, store, page, tmp_path):
    prepare(store)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app)
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    for width, height in [(1440, 1000), (390, 850), (320, 640), (800, 480)]:
        page.set_viewport_size({"width": width, "height": height})
        for tab in ["Vibe", "Genre focus", "Nerd out"]:
            dialog.get_by_role("tab", name=tab, exact=True).click()
            expect(dialog.get_by_role("button", name="Reset mix adjustments", exact=True)).to_be_in_viewport()
            expect(dialog.get_by_role("button", name="Close mix controls", exact=True)).to_be_in_viewport()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
            panel = dialog.locator('[role="tabpanel"]:visible')
            expect(panel).to_have_count(1)
            assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
            if tab == "Vibe":
                expect(dialog.locator('.fp-vibe-art')).to_have_count(7)
                page.wait_for_function("""() => [...document.querySelectorAll('.fp-vibe-art')]
                    .every(image => image.complete && image.naturalWidth === 480)""")
                natural = dialog.locator('.fp-vibe-card-natural .fp-vibe-content')
                assert natural.evaluate("""card => {
                    const art = card.querySelector('img').getBoundingClientRect();
                    const bounds = card.getBoundingClientRect();
                    const label = card.querySelector('strong').getBoundingClientRect();
                    return art.width >= bounds.width - 2 && art.height >= bounds.height - 2
                        && label.left >= art.left && label.right <= art.right
                        && label.top >= art.top && label.bottom <= art.bottom;
                }""")
                expect(dialog).to_have_css('background-image', 'none')
                last = dialog.get_by_role("radio", name="Let off steam", exact=True)
                last.scroll_into_view_if_needed()
                expect(last).to_be_in_viewport()
                expect(dialog.get_by_role("button", name="Reset mix adjustments", exact=True)).to_be_in_viewport()
                expect(dialog.get_by_role("button", name="Close mix controls", exact=True)).to_be_in_viewport()
            page.screenshot(path=tmp_path / f"tuning-{tab.lower().replace(' ', '-')}-{width}.png")
        # Native disclosures keep genre adjustments handy and eras out of the initial view.
        genres = dialog.locator("details").filter(has=page.get_by_text("Your genre balance", exact=True))
        eras = dialog.locator("details").filter(has=page.get_by_text("A little time travel", exact=True))
        expect(genres).to_have_attribute("open", "")
        expect(eras).not_to_have_attribute("open", "")
        eras.locator("summary").click()
        expect(eras.get_by_role("slider")).to_have_count(5)
        eras.locator("summary").click()
        # The genre name is a keyboard disclosure; its children keep the parent slider alignment.
        toggle = dialog.locator('.fp-genre[data-fam="techno"] .fp-genre-toggle')
        toggle.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        subgenres = dialog.locator("#" + toggle.get_attribute("aria-controls"))
        expect(subgenres).to_be_visible()
        positions = dialog.locator('.fp-genre[data-fam="techno"] .fp-slider-wrap').evaluate_all(
            "els => els.map(el => el.getBoundingClientRect().x)")
        assert max(positions) - min(positions) < 1
        panel = dialog.locator('[role="tabpanel"]:visible')
        assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
        expect(dialog.get_by_role("button", name="Reset mix adjustments", exact=True)).to_be_in_viewport()
        page.screenshot(path=tmp_path / f"tuning-subgenres-{width}.png")
        toggle.press("Space")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(subgenres).not_to_be_visible()
    # Arrow navigation visits all three tabs and wraps. Home/End remain useful.
    tab = dialog.get_by_role("tab", name="Nerd out", exact=True)
    tab.press("ArrowRight")
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(dialog.get_by_role("tab", name="Genre focus", exact=True)).to_be_focused()
    page.keyboard.press("End")
    expect(tab).to_be_focused()
    page.keyboard.press("Home")
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_be_focused()


def test_open_vibe_tab_resets_when_tuning_expires(live_app, store, page):
    prepare(store)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.clock.install()
    page.goto(live_app)
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    dialog.get_by_role("tab", name="Vibe", exact=True).click()
    dialog.get_by_role("radio", name="Let off steam", exact=True).check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "release")
    page.get_by_role('button', name='Change vibe', exact=True).click()
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response("**/home/fingerprint"):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[role="tabpanel"]:visible')).to_have_count(1)
    expect(dialog.locator('#fp-preferences-panel')).not_to_be_visible()
    expect(page.locator('#home-vibe-feedback')).not_to_be_visible()


def test_vibe_respects_genre_focus_through_clear_and_expiry(live_app, store, page):
    prepare(store)
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
    dialog.get_by_role('radio', name='Take it as it comes', exact=True).check()
    expect(row).to_have_attribute('data-vibe', '')
    expect(page.locator('.fp-tuning-dot')).to_be_visible()  # The independent Jazz focus remains active.
    vibe_dot = page.locator('#fp-vibe-tab .fp-tab-dot')
    expect(vibe_dot).to_have_js_property('hidden', True)
    expect(vibe_dot).not_to_be_visible()
    assert row.locator('[data-genres]').evaluate_all(
        'els => els.length > 0 && els.every(el => JSON.parse(el.dataset.genres).every(g => g.toLowerCase() === "jazz"))')
    page.get_by_role('button', name='Change vibe', exact=True).click()
    dialog.get_by_role('radio', name='Relax', exact=True).check()
    expect(row).to_have_attribute('data-vibe', 'relax')
    store.rec_model.set_lean('genre:techno', 1.6, 1)
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response('**/home/fingerprint'):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(row).to_have_attribute('data-vibe', '')
    assert store.rec_model.get_lean('genre:techno') == 1.6
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']
