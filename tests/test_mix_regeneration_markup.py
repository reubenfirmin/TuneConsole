"""Both real template variants wire the shared regeneration handler correctly."""
from html.parser import HTMLParser
import json

from fastapi.testclient import TestClient
import pytest

from tests.home_fixture_support import publish_menu
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
    publish_menu(store, 1, modeled=modeled)
    app = create_app(store, lambda: {}, now_fn=lambda: 1)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
    assert response.status_code == 200
    buttons = RefreshButtons(response.text).buttons
    assert len(buttons) == 4
    for button in buttons:
        lane = button['hx-target'].removeprefix('#gen-body-')
        assert button['hx-post'] == ('/home/refresh-cards' if modeled else f'/home/refresh-card/{lane}')
        assert button['hx-select'] == button['hx-target']
        assert button['hx-swap'] == 'outerHTML'
        assert button['hx-trigger'] == 'mix-regenerate'
        assert button['hx-disabled-elt'] == 'this'
        assert button['@click.stop'] == 'regenerateMix($el)'
        if modeled:
            assert json.loads(button['hx-vals'])['opportunity_id']
