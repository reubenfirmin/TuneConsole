"""The inexpensive status poll exposes persistent failures and recovery."""
import json

import pytest

from tests.taste_support import _client
from yt_playlist.rec import progressive


@pytest.mark.parametrize('reason', ['matrix-memory-budget', 'corpus-memory-budget', 'fit-time-budget'])
@pytest.mark.parametrize('existing_model', [False, True])
def test_persistent_failure_is_visible_and_recovery_clears_it(store, monkeypatch, reason, existing_model):
    client = _client(store)
    if existing_model:
        store.settings.set_setting('rec_content_model', '{"ncat":1,"cat":{"fam:techno":0},"cont":[]}')
    store.settings.set_setting(progressive.FIT_STATE, json.dumps({
        'state': 'deferred', 'reason': reason, 'attempts': 3, 'next_attempt_at': 1900}))

    def unexpected(*args, **kwargs):
        raise AssertionError('Status polling performed preparation or heavy model reads')

    monkeypatch.setattr(progressive.Preparation, 'fit', unexpected)
    monkeypatch.setattr(progressive.Preparation, 'repair', unexpected)
    monkeypatch.setattr(store.rec_evidence, 'rec_baskets', unexpected)
    monkeypatch.setattr(store.prepared_content, 'corpus', unexpected)
    response = client.get('/taste/content-maintenance')
    assert response.status_code == 200
    assert 'role="alert"' in response.text and 'retries every 15 minutes' in response.text
    assert ('last completed model remains available' in response.text) == existing_model
    assert ('first genre and audio model is still waiting' in response.text) != existing_model
    store.settings.set_setting(progressive.FIT_STATE, '{"state":"ready"}')
    response = client.get('/taste/content-maintenance')
    assert response.status_code == 200 and 'role="alert"' not in response.text
    assert 'every 30s' in response.text


def test_health_panel_includes_automatic_maintenance_status(store):
    client = _client(store)
    store.settings.set_setting(progressive.FIT_STATE,
                               '{"state":"deferred","reason":"matrix-memory-budget"}')
    response = client.get('/taste/panels/health')
    assert response.status_code == 200
    assert 'Genre and audio model refresh is delayed' in response.text
    assert 'hx-get="/taste/content-maintenance"' in response.text
