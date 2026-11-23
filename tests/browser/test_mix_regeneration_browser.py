import json

import pytest
from playwright.sync_api import expect

from tests.group_support import publish_keyed
from tests.home_fixture_support import returning_listener
from tests.onboarding_support import publish_live_mix_inputs
from yt_playlist.rec import rec_params


@pytest.mark.browser
def test_replacement_mix_uses_its_own_listening_offer(live_app, page, store):
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    store.settings.set_setting('home_format', 'mixes')
    returning_listener(store, 'mixes')
    tracks = publish_live_mix_inputs(store, now=1, tracks_per_lane=24)
    rec_params.set_param(store, 'comfort_min_pool', 0)
    publish_keyed(store, [
        (genre, [track['key'] for track in tracks if track['video_id'].startswith(f'live-{lane}-')], character)
        for lane, genre, character in (
            ('wheelhouse', 'House', .25), ('explore', 'Jazz', .75), ('comfort', 'Ambient', .05))
    ], now=1)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.route('https://i.ytimg.com/**', lambda route: route.abort())

    def is_listening_response(response, kind):
        return (response.url.endswith('/home/listening-response')
                and response.request.post_data_json['kind'] == kind)

    with page.expect_response(lambda response: is_listening_response(response, 'visible')) as initial:
        page.goto(live_app + '/?panel=mixes')
    assert initial.value.ok
    card = page.locator('#gen-wheelhouse')
    body = card.locator('#gen-body-wheelhouse')
    original = json.loads(body.get_attribute('data-recipe'))
    assert initial.value.request.post_data_json['opportunity_id'] == original['opportunity_id']

    siblings = page.locator('#home-mode-cards .gen-card[data-choice-opportunity]:not(#gen-wheelhouse)')
    read_identities = """cards => cards.map(card =>
        [card.id, card.dataset.choiceOpportunity, card.dataset.choiceOption])"""
    sibling_identities = siblings.evaluate_all(read_identities)
    assert len(sibling_identities) == 3

    card.locator('.gen-poster').click()
    button = card.get_by_role('button', name='Regenerate this mix')
    expect(button).to_have_attribute('hx-post', '/home/refresh-cards')
    with page.expect_response(lambda response: is_listening_response(response, 'visible')
            and response.request.post_data_json['opportunity_id'] != original['opportunity_id']) as visible:
        with page.expect_response('**/home/refresh-cards') as regenerated:
            button.click()
        assert regenerated.value.ok
        expect(button).to_be_enabled()
        expect(card).not_to_have_attribute('data-choice-opportunity', original['opportunity_id'], timeout=6000)
        card.locator('.gen-back').click()

    replacement = json.loads(body.get_attribute('data-recipe'))
    identity = {key: replacement[key] for key in ('opportunity_id', 'option_id')}
    assert identity['opportunity_id'] != original['opportunity_id']
    expect(card).to_have_attribute('data-choice-opportunity', identity['opportunity_id'])
    expect(card).to_have_attribute('data-choice-option', identity['option_id'])
    assert visible.value.ok
    assert visible.value.request.post_data_json == {**identity, 'kind': 'visible'}
    assert siblings.evaluate_all(read_identities) == sibling_identities

    with page.expect_response(lambda response: is_listening_response(response, 'open')) as opened:
        card.locator('.gen-poster').click()
    assert opened.value.ok
    assert opened.value.request.post_data_json == {**identity, 'kind': 'open'}
