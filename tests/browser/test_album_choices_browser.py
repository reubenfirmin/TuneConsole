import pytest
from urllib.parse import parse_qs

from tests.test_mode_cards_route import prepare_home as prepare_current_home
from tests.home_fixture_support import browser_store, returning_listener
from tests.prepared_fixture_support import prepared_template
from tests.browser.quiet_action_support import (  # noqa: F401 - shared evidence fixture
    action_evidence, assert_quiet_action, fail_action,
)


def prepare_home(store, now=1):
    returning_listener(store, 'mixes', 'albums', now=now)
    prepare_current_home(store, now)


@pytest.fixture(scope='module')
def album_home_template(_pristine_current_schema, genre_candidates_template):
    def build(source):
        genre_candidates_template.restore(source)
        # Match live_app's pre-preparation account/playlist state. Each live
        # consumer later upserts these identities in its own private copy.
        identity = source.accounts.upsert_account('main', 'cred', None, True)
        source.playlists.upsert_playlist(identity, 'PLZ', 'Old Mix', 3, 'h', 0.0)
        source.settings.set_setting('last_sync_at', '1')
        source.settings.set_setting('onboard_dismissed', '1')
        prepare_home(source, 1)
        assert not source.rec_proposals.get_proposals('album_prepared')

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def store(browser_store, album_home_template):
    # Restore before live_app starts. Home is prepared, but albums, visibility
    # responses, choices, notes and all subsequent mutations belong to the case.
    album_home_template.restore(browser_store)
    return browser_store


@pytest.mark.parametrize("width", [1440])
def test_album_tab_visibility_and_remembered_default(live_app, store, page, width, monkeypatch):
    import json
    import time
    from playwright.sync_api import expect
    from tests.onboarding_support import publish_personal_mix

    returning_listener(store, 'radio', now=1)
    tracks = publish_personal_mix(store, now=1)
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'menu_id': 'album-preference-radio', 'tracks': tracks[:4]}))
    albums = [{"browse_id": f"album-{index}", "title": f"Record {index}",
        "artist": f"Artist {index}", "year": "2020", "genre": "Jazz", "thumbnail": None,
        "classification": "discovery", "score": 1 - index / 10} for index in range(4)]
    albums[0]['title'] = 'Global Underground #43: Rotterdam (Unmixed) · A very long album title'
    albums[0]['rationale'] = {'text': 'A possible fit for your “Late nights” playlist.'}
    store.wiki.put('artist:artist 0', 'artist', 'Artist 0', {
        'title': 'Artist 0', 'extract': 'A distinct voice in electronic music.',
        'url': 'https://en.wikipedia.org/wiki/Artist_0'}, 1)
    from tests.conftest import FakeClient
    from tests.test_album_web import _album
    monkeypatch.setattr(FakeClient, 'get_album', lambda self, browse: _album()["MPREb_x"])
    store.rec_proposals.publish_album_note('album-0', {'description': {
        'text': 'An album shaped by intricate rhythms and patient melodic development.',
        'source': 'Discogs release notes', 'url': 'https://www.discogs.com/release/123',
        'acquisition': 'automatic'}, 'rating': {
        'value': 4.3, 'count': 28, 'scale': 5, 'source': 'Discogs community',
        'url': 'https://www.discogs.com/release/123'}, 'retry_at': 1000}, 1)
    for index in range(1, 4):
        store.rec_proposals.publish_album_note(f'album-{index}', {'artwork': {
            'url': f'https://album-images.test/scene-{index}.svg',
            'source_url': 'https://example.com/photographer', 'kind': 'artist',
            'credit': f'Artist {index} · Photographer · CC BY-SA 4.0'}, 'retry_at': 1000}, 1)
    page.route('https://album-images.test/*', lambda route: route.fulfill(
        content_type='image/svg+xml', body='''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><defs><linearGradient id="g"><stop stop-color="#214653"/><stop offset="1" stop-color="#a67c57"/></linearGradient></defs><path fill="url(#g)" d="M0 0h1200v800H0z"/><circle cx="600" cy="260" r="130" fill="#c3ab8f"/><path d="M290 800v-120a310 310 0 0 1 620 0v120" fill="#253b42"/></svg>'''))
    store.rec_proposals.put_proposals("album_prepared", {'items': albums, 'context': {}, 'built_at': 1}, 1)
    page.set_viewport_size({"width": width, "height": 1100})
    page.goto(live_app + "/", wait_until='domcontentloaded')
    expect(page.locator("#home-album-choices")).to_be_hidden()
    # A hidden tab was rendered, but browser visibility must not be invented.
    assert not [row for row in store.rec_ledger.evidence().records("response")
                if row["kind"] == "visible" and row["option_id"].startswith("album-")]
    with page.expect_response(lambda response: response.url.endswith('/home/listening-response')
            and response.request.post_data_json.get('kind') == 'visible'
            and response.request.post_data_json.get('option_id', '').startswith('album-')) as visible:
        page.get_by_role("tab", name="Long play", exact=True).click()
    assert visible.value.ok
    # Visible cards post concurrently; receiving the first response does not
    # establish that the other observations have finished writing.
    deadline = time.monotonic() + 5
    while True:
        observations = store.rec_ledger.evidence().records('response')
        if any(row['kind'] == 'visible' and row['option_id'].startswith('album-')
               for row in observations) or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(25)
    assert any(row['kind'] == 'visible' and row['option_id'].startswith('album-')
               for row in observations), (visible.value.request.post_data_json, observations)
    expect(page.locator(".album-choice")).to_have_count(4)
    expect(page.locator('.album-story-scene img')).to_have_count(3)
    expect(page.locator('.album-choice-play svg:visible')).to_have_count(4)
    expect(page.locator('.album-choice-play')).to_have_text(['', '', '', ''])
    expect(page.locator('.album-choice-facts .gtag')).to_have_count(4)
    expect(page.locator('.album-familiarity')).to_have_count(4)
    expect(page.get_by_role('meter')).to_have_count(0)
    expect(page.get_by_text('Pick a record', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Another set', exact=True)).to_have_count(0)
    alignment = page.locator('.dash-navigation').evaluate("""el => {
      const tab = el.querySelector('[role="tab"][aria-selected="true"]').getBoundingClientRect();
      const icon = el.querySelector('.album-tune').getBoundingClientRect();
      return Math.abs(tab.top + tab.height / 2 - icon.top - icon.height / 2);
    }""")
    assert alignment < 2
    page.screenshot(path=f'/tmp/tuneconsole-albums-initial-{width}.png', full_page=True, animations='disabled')
    overflow = page.evaluate("""() => Array.from(document.querySelectorAll('#home-album-choices *')).filter(el => {
      const box = el.getBoundingClientRect(); return box.width && box.right > innerWidth + 1;
    }).map(el => ({tag: el.tagName, cls: el.className, right: el.getBoundingClientRect().right})).slice(-15)""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), overflow
    frames = page.locator('.album-choice').evaluate_all("""cards => cards.map(card => {
        const box = card.getBoundingClientRect();
        return {left:box.left, top:box.top, right:box.right, bottom:box.bottom};
    })""")
    assert all(frames[i]['bottom'] <= frames[i + 1]['top'] for i in range(3))
    if width > 1000:
        assert page.locator('.album-choice--reverse .album-sleeve').bounding_box()['x'] > page.locator('.album-choice--reverse .album-story-copy').bounding_box()['x']
        assert page.locator('.album-choice--inset .album-story-scene').bounding_box()['width'] < 160
    covers = page.locator('.album-choice-art').evaluate_all('nodes => nodes.map(el => el.getBoundingClientRect().width)')
    assert max(covers) - min(covers) < 1
    fonts = page.locator('.album-choice-title').evaluate_all('nodes => nodes.map(el => getComputedStyle(el).fontSize)')
    assert len(set(fonts)) == 1
    expect(page.locator('.album-story-kicker, .album-collection-mark')).to_have_count(0)
    card = page.locator('[data-choice-option="album-0"]')
    expect(card.locator('.album-sleeve-notes')).to_be_visible()
    expect(card.locator('.album-record-note')).to_contain_text('intricate rhythms')
    expect(card.locator('.album-fit-note')).to_have_text('Why you might like it: A possible fit for your “Late nights” playlist.')
    expect(card.locator('.album-note-rating')).to_have_attribute('aria-label', 'Discogs community: 4.3 out of 5, 28 ratings')
    expect(card.locator('.album-stars-base svg')).to_have_count(5)
    expect(card.locator('.album-stars-fill')).to_have_attribute('style', 'width:86.0%')
    expect(card.locator('a[href*="musicbrainz.org"], a[href*="wikipedia.org"]')).to_have_count(0)
    expect(page.get_by_text('Looking up sleeve notes', exact=False)).to_have_count(0)
    expect(card.locator('.album-inline')).to_be_hidden()
    card.locator('.album-choice-art').click()
    expect(card.locator('.album-inline .track-table tbody tr.srow')).to_have_count(2)
    expect(card.locator('.album-inline')).to_be_visible()
    expect(card.locator('.album-inline .album-library')).to_have_count(1)
    expect(card.locator('.album-inline').get_by_role('button', name='Save album to library', exact=True)).to_be_visible()
    expect(card.locator('.album-inline-feedback')).to_have_count(0)
    expect(card.get_by_role('link', name='Album page', exact=True)).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert card.locator('.track-table').evaluate('''table =>
        Math.max(...[...table.querySelectorAll('thead th')].map(th => th.getBoundingClientRect().bottom)) <=
        table.querySelector('tbody tr').getBoundingClientRect().top + 1''')
    card.locator('.album-inline th').filter(has_text='Song').click()
    expect(card.locator('.album-inline .sort-indicator.is-ascending')).to_have_count(1)
    assert page.url == live_app + '/#album-choices'
    card.get_by_role('button', name='Close album', exact=True).click()
    expect(card.locator('.album-inline')).to_be_hidden()
    card.locator('.album-choice-title button').click()
    expect(card.locator('.album-inline .track-table tbody tr.srow')).to_have_count(2)
    page.screenshot(path=f'/tmp/tuneconsole-albums-{width}.png', full_page=True, animations='disabled')
    # Exercise actual htmx request IDs: two deliberate clicks from one held menu differ.
    requests = []
    def capture(route):
        requests.append(parse_qs(route.request.post_data))
        route.fulfill(status=204)
    page.route('**/album/play', capture)
    for _ in range(2):
        with page.expect_response(lambda response: response.url.endswith('/album/play')):
            page.locator('.album-choice-play').first.click()
    assert len(requests) == 2
    assert requests[0]['action_id'][0] != requests[1]['action_id'][0]
    assert requests[0]['opportunity_id'] == requests[1]['opportunity_id']
    # The explicit preference is shared across visits, including fresh URLs without a hash.
    assert all(page.request.get(live_app + '/home/growth').json()['ready_formats'].values())
    assert page.evaluate("""async () => (await fetch('/home/format', {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'format=albums'})).status""") == 204
    assert store.settings.get_setting('home_format') == 'albums'
    page.goto(live_app + "/", wait_until='domcontentloaded')
    expect(page.get_by_role("tab", name="Long play", exact=True)).to_have_attribute("aria-selected", "true")
    card.locator('.album-choice-art').click()
    artist = card.locator('.album-inline tbody tr.srow a.alink').first
    target = artist.get_attribute('href')
    artist.click()
    expect(page).to_have_url(live_app + target)


@pytest.mark.parametrize('delayed', [False, True])
def test_album_tuner_keyboard_apply_reset_and_recorded_policy(live_app, store, page, delayed, action_evidence, monkeypatch):
    from playwright.sync_api import expect
    from tests.rec.test_album_menu import records
    from yt_playlist.rec import album_tuning, choice_evidence

    for album in records()[:6]:
        store.collection.add_saved_album({**album, 'browse': album['browse_id']})
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': 1, 'context': {}, 'items': records()}, 1)
    pending = []
    from yt_playlist.web.routes import home_discovery
    album_context = home_discovery.album_context
    if delayed:
        # Exercise the genuine lazy-fragment path even when the normal Home
        # response can already embed this fixture's prepared album board.
        monkeypatch.setattr(home_discovery, 'album_context', lambda *args, **kwargs: None)
        page.route('**/home/album-choices', lambda route: pending.append(route))
    page.set_viewport_size({'width': 1440, 'height': 1100})
    page.goto(live_app + '/#album-choices', wait_until='domcontentloaded')
    tune = page.get_by_role('button', name='Adjust album parameters')
    if delayed:
        monkeypatch.setattr(home_discovery, 'album_context', album_context)
        expect(tune).to_be_visible()
        expect(tune).to_be_disabled()
        assert len(pending) == 1
        pending[0].continue_()
    tune.click()
    dialog = page.get_by_role('dialog', name='Long play')
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('slider')).to_have_count(1)
    expect(dialog.get_by_text('Use these settings for 4 hours.', exact=True)).to_be_visible()
    slider = dialog.get_by_role('slider', name='Comfort to Novelty')
    slider.focus()
    slider.press('End')
    dialog.get_by_label('Jazz', exact=True).check()
    failed = []
    for kind in ('plaintext', 'network', 'json'):
        def unavailable(route, kind=kind):
            failed.append(parse_qs(route.request.post_data))
            fail_action(route, kind)
        page.route('**/home/album-choices/tune', unavailable)
        dialog.get_by_role('button', name='Apply', exact=True).click()
        expect(dialog.get_by_role('status')).to_have_text('Changes not confirmed. Your choices are kept here.')
        expect(dialog.get_by_role('button', name='Apply', exact=True)).to_be_enabled()
        expect(slider).to_have_value('100')
        expect(dialog.get_by_label('Jazz', exact=True)).to_be_checked()
        assert_quiet_action(page)
        page.unroute('**/home/album-choices/tune', unavailable)
    assert len(failed) == 3
    assert all(body['novelty'] == ['100'] and body['genre'] == ['jazz'] for body in failed)
    page.screenshot(path=action_evidence + '.png')
    page.screenshot(path='/tmp/tuneconsole-album-tuner.png', animations='disabled')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(tune).to_be_focused()
    expect(page.locator('.album-choice[data-classification="discovery"], .album-choice[data-classification="familiar_artist"]')).to_have_count(3)
    assert album_tuning.state(store, 1)['values']['novelty'] == 1
    offers = store.rec_ledger.evidence().records('opportunity')
    offer = next(row for row in offers if row['format'] == 'albums'
                 and all(item['genre'] == 'Jazz' for item in row['options']))
    policy = choice_evidence.resolve_context(store, {'artifact': offer['context']})['album_menu']
    assert policy['genres'] == ['jazz'] and policy['discovery_requested'] == 4
    assert policy['discovery_offered'] == 3
    old_offer = offer['id']
    tune.click()
    dialog.get_by_role('button', name='Refresh selection', exact=True).click()
    expect(dialog).not_to_be_visible()
    assert any(row['id'] != old_offer
               for row in store.rec_ledger.evidence().records('opportunity') if row['format'] == 'albums')
    tune.click()
    dialog.get_by_role('button', name='Reset', exact=True).click()
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).not_to_be_visible()
    assert album_tuning.state(store, 1)['remaining'] == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_album_tab_recovers_when_preparation_finishes_after_initial_load(live_app, store, page):
    from playwright.sync_api import expect

    assert not store.rec_proposals.get_proposals('album_prepared')
    page.goto(live_app + '/', wait_until='domcontentloaded')
    page.get_by_role('tab', name='Long play', exact=True).click()
    expect(page.locator('#home-album-choices')).to_have_attribute('hx-trigger', 'every 3s')
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': 1, 'context': {},
        'items': [{'browse_id': f'late-{index}', 'title': f'Late record {index}',
                   'artist': f'Artist {index}', 'year': '2020', 'genre': 'Jazz',
                   'classification': 'discovery', 'score': 1 - index / 10}
                  for index in range(4)]}, 1)
    expect(page.locator('.album-choice')).to_have_count(4, timeout=8000)
    expect(page.locator('#home-album-choices')).not_to_have_attribute('hx-trigger', 'every 3s')
