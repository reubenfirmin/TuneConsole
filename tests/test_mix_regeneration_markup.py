"""Both real template variants wire the shared regeneration handler correctly."""
from html.parser import HTMLParser
import json

from fastapi.testclient import TestClient
import pytest

from tests.group_support import publish_keyed
from tests.onboarding_support import publish_live_mix_inputs
from yt_playlist.rec import rec_params
from yt_playlist.web.app import create_app


class RefreshButtons(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.buttons = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'button' and attrs.get('aria-label') == 'Regenerate this mix':
            self.buttons.append(attrs)


@pytest.mark.parametrize('modeled', [False, True])
def test_regeneration_template_wiring(store, unlocked_home_controls, modeled):
    tracks = publish_live_mix_inputs(store, tracks_per_lane=20 if modeled else 4)
    modeled_lanes = {'wheelhouse', 'explore', 'comfort'} if modeled else set()
    if modeled:
        rec_params.set_param(store, 'comfort_min_pool', 0)
        publish_keyed(store, [
            (genre, [track['key'] for track in tracks if track['video_id'].startswith(f'live-{lane}-')], character)
            for lane, genre, character in (
                ('wheelhouse', 'House', .25), ('explore', 'Jazz', .75), ('comfort', 'Ambient', .05))
        ], now=1000)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
    assert response.status_code == 200
    buttons = RefreshButtons(response.text).buttons
    assert len(buttons) == 4
    assert {button['hx-target'] for button in buttons} == {
        f'#gen-body-{lane}' for lane in ('wheelhouse', 'explore', 'comfort', 'fresh')}
    protos = {proto['lane']: proto for proto in response.context['protos']}
    for button in buttons:
        lane = button['hx-target'].removeprefix('#gen-body-')
        modeled_card = lane in modeled_lanes
        assert button['hx-post'] == ('/home/refresh-cards' if modeled_card else f'/home/refresh-card/{lane}')
        assert button['hx-select'] == button['hx-target']
        assert button['hx-swap'] == 'outerHTML'
        assert button['hx-trigger'] == 'mix-regenerate'
        assert button['hx-disabled-elt'] == 'this'
        assert button['@click.stop'] == 'regenerateMix($el)'
        if modeled_card:
            assert protos[lane]['recipe']['model'] == 'musical-group'
            opportunity = protos[lane]['recipe']['opportunity_id']
            assert opportunity and json.loads(button['hx-vals']) == {'opportunity_id': opportunity}
        else:
            assert 'hx-vals' not in button
