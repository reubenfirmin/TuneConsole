"""Home retains the visible menu assignment without historical track recipes (#688)."""
import html
import json
import re

from fastapi import APIRouter
from fastapi.testclient import TestClient

from tests.rec.test_home_prepared import (
    one_menu_per_attempt, publication_inputs, publication_prepared, store,
)


def test_home_keeps_assignment_and_visible_options_without_hidden_ranker_recipes(
        store, publication_prepared, unlocked_home_controls, monkeypatch):
    from yt_playlist.rec import choice_evidence
    from yt_playlist.core import mix_choices
    from yt_playlist.web.app import create_app
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home

    # The resource consumes a completed menu. Its ranking is controlled below
    # preparation; real HTTP rendering, evidence capture and SQL expansion own
    # the visible assignment and omission of private recipe fields here.
    monkeypatch.setattr(webapp, 'build_all',
                        lambda ctx: [app_update.build(ctx), audio_preview.build(ctx), home.build(ctx)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
    assert response.status_code == 200
    match = re.search(r"hx-vals='([^']*opportunity_id[^']*)'", response.text)
    assert match, 'The rendered menu must identify its actual choice opportunity'
    identity = json.loads(html.unescape(match[1]))['opportunity_id']
    offer = choice_evidence.opportunity(store, identity)
    assert offer and offer['format'] == 'mixes' and len(offer['options']) > 1
    assert [option['position'] for option in offer['options']] == list(range(1, len(offer['options']) + 1))
    for option in offer['options']:
        visible = mix_choices.expand(store.conn, option)
        assert visible['label'] and visible['intent']
        assert option['assignment']['unit'] == 'whole_menu'
        assert 0 < option['assignment']['probability'] <= 1
        assert not {'tracks', 'recipe', 'mode_id', 'ranker', 'centroid', 'vectors'} & option.keys()
    # Theme visibility does not claim exposure to any of its possible songs.
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 0
