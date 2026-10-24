import pytest
from urllib.parse import parse_qs

from tests.test_mode_cards_route import prepare_home
from tests.test_home_other_genres import seed_genres


@pytest.mark.parametrize("width", [1440, 390])
def test_album_tab_visibility_and_remembered_default(live_app, store, page, width, monkeypatch):
    from playwright.sync_api import expect

    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    prepare_home(store, 1)
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
    store.rec_proposals.put_proposals("album_prepared", {'items': albums, 'context': {}, 'built_at': 1}, 1)
    page.set_viewport_size({"width": width, "height": 1100})
    page.goto(live_app + "/")
    expect(page.locator("#home-album-choices")).to_be_hidden()
    # A hidden tab was rendered, but browser visibility must not be invented.
    assert not [row for row in store.rec_ledger.evidence().records("response")
                if row["kind"] == "visible" and row["option_id"].startswith("album-")]
    with page.expect_response(lambda response: response.url.endswith('/home/listening-response')
            and response.request.post_data_json.get('kind') == 'visible'
            and response.request.post_data_json.get('option_id', '').startswith('album-')) as visible:
        page.get_by_role("tab", name="Long play", exact=True).click()
    assert visible.value.ok
    assert any(row['kind'] == 'visible' and row['option_id'].startswith('album-')
               for row in store.rec_ledger.evidence().records('response'))
    expect(page.locator(".album-choice")).to_have_count(4)
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
    page.evaluate("""() => fetch('/home/format', {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'format=albums'})""")
    page.goto(live_app + "/")
    expect(page.get_by_role("tab", name="Long play", exact=True)).to_have_attribute("aria-selected", "true")
    card.locator('.album-choice-art').click()
    artist = card.locator('.album-inline tbody tr.srow a.alink').first
    target = artist.get_attribute('href')
    artist.click()
    expect(page).to_have_url(live_app + target)


def test_album_tuner_keyboard_apply_reset_and_recorded_policy(live_app, store, page):
    from playwright.sync_api import expect
    from tests.rec.test_album_menu import records
    from yt_playlist.rec import album_tuning, choice_evidence

    seed_genres(store)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    prepare_home(store, 1)
    for album in records()[:6]:
        store.collection.add_saved_album({**album, 'browse': album['browse_id']})
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': 1, 'context': {}, 'items': records()}, 1)
    page.set_viewport_size({'width': 390, 'height': 1100})
    page.goto(live_app + '/#album-choices')
    tune = page.get_by_role('button', name='Adjust album parameters')
    tune.click()
    dialog = page.get_by_role('dialog', name='Long play')
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('slider')).to_have_count(1)
    expect(dialog.get_by_text('Use these settings for 4 hours.', exact=True)).to_be_visible()
    slider = dialog.get_by_role('slider', name='Comfort to Novelty')
    slider.focus()
    slider.press('End')
    dialog.get_by_label('Jazz', exact=True).check()
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

    seed_genres(store)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    prepare_home(store, 1)
    page.goto(live_app + '/')
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
