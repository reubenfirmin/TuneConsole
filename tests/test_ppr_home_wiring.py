"""Live Home retains visible choice evidence without private ranker inputs."""
import re

from fastapi.testclient import TestClient

from tests.group_support import publish_keyed
from tests.onboarding_support import publish_live_mix_inputs
from yt_playlist.core import mix_choices
from yt_playlist.rec import choice_evidence, home_cards, rec_params
from yt_playlist.web.app import create_app


def test_home_keeps_assignment_and_visible_options_without_hidden_ranker_recipes(
        store, unlocked_home_controls, monkeypatch):
    tracks = publish_live_mix_inputs(store, tracks_per_lane=20)
    rec_params.set_param(store, 'comfort_min_pool', 0)
    publish_keyed(store, [
        (genre, [track['key'] for track in tracks if track['video_id'].startswith(f'live-{lane}-')], character)
        for lane, genre, character in (
            ('wheelhouse', 'House', .25), ('explore', 'Jazz', .75), ('comfort', 'Ambient', .05))
    ], now=1000)

    # Supply known assignment metadata on the actual live selection to check
    # how the HTTP and evidence paths carry it.
    assignment = {'policy': 'controlled-test', 'unit': 'whole_menu', 'menu': 1, 'probability': .3,
                  'candidates': [{'menu': 0, 'probability': .7}, {'menu': 1, 'probability': .3}]}
    live_cards = home_cards.HomeCards.cards

    def assigned_cards(service, request):
        result = live_cards(service, request)
        for proto in result.protos:
            proto.update(assignment=assignment, ranker='private-ranker')
            proto['counterfactual'] = {'tracks': list(reversed(proto['tracks']))}
        return result

    monkeypatch.setattr(home_cards.HomeCards, 'cards', assigned_cards)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
    assert response.status_code == 200
    displayed = re.findall(
        r'id="gen-([^"]+)"\s+data-choice-opportunity="([^"]+)" data-choice-option="([^"]+)"',
        response.text)
    assert [lane for lane, _, _ in displayed] == ['wheelhouse', 'explore', 'fresh', 'comfort']
    identity = displayed[0][1]
    assert {opportunity for _, opportunity, _ in displayed} == {identity}
    offer = choice_evidence.opportunity(store, identity)
    assert offer and offer['format'] == 'mixes'
    options = [mix_choices.expand(store.conn, option) for option in offer['options']]
    assert [(option['intent'], option['id']) for option in options] == [
        (lane, option) for lane, _, option in displayed]
    assert [option['position'] for option in options] == [1, 2, 3, 4]
    labels = {proto['lane']: proto['label'] for proto in response.context['protos']}
    for option in options:
        assert option['label'] == labels[option['intent']]
        assert option['assignment'] == assignment
        assert not {'tracks', 'recipe', 'mode_id', 'ranker', 'counterfactual', 'centroid', 'vectors'} & option.keys()
    # Theme visibility does not claim exposure to any of its possible songs.
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 0
