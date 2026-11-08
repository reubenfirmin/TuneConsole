import numpy as np
import pytest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store
from tests.home_fixture_support import home_routes_only, publish_menu, without_metadata_providers

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'home_routes_only', 'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'))) as schema:
        yield schema


@pytest.fixture(autouse=True)
def without_diagnostic_history(monkeypatch):
    from yt_playlist.rec.home_cards import HomeCards

    # Real impressions/picks are the subject here. Audit metadata and JSONL
    # capture have their own route-level owner in test_home_audit.
    monkeypatch.setattr(HomeCards, 'audit', lambda *args, **kwargs: None)


def _seed_modes(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
        {"mode_id": 2, "label": "b", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 60, "rep_keys": []},
    ], retired_ids=[], now=1.0)


def _client(store, now=1000.0):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now,
                                 background_work=False),
                      base_url="http://127.0.0.1")


def test_cards_render_logs_impressions(store):
    _seed_modes(store)
    # Serving consumes a completed menu. Ranking and pool production have
    # independent owners; real persistence and impression recording stay here.
    publish_menu(store, count=2, mode_ids=(1, 2))
    client = _client(store)
    r = client.get("/home/cards")
    assert r.status_code == 200
    impressions = store.modes.impression_counts()
    assert impressions == {1: 1, 2: 1}
    assert "mode_id" in r.text       # the recipe carrying mode_id is embedded for Save & play
    assert client.get("/home/cards").status_code == 200
    assert store.modes.impression_counts() == impressions


@pytest.fixture
def completed_generated_playlist(store, monkeypatch):
    from yt_playlist.library import playlist_generated

    # The route owns mode-pick logging. Remote creation, item ingestion and
    # quarantine have their own generated-playlist owners.
    account = store.accounts.upsert_account('main', 'cred', None, True)
    playlist = store.playlists.upsert_playlist(account, 'created-mix', 'My Mix', 1, 'hash', 1000)
    create = Mock(return_value={'new_ytm': 'created-mix', 'pid': playlist, 'added': 1})
    monkeypatch.setattr(playlist_generated, 'create_generated_playlist', create)
    return create


def test_generate_logs_pick(store, completed_generated_playlist):
    _seed_modes(store)
    r = _client(store).post("/home/generate", data={
        "name": "My Mix",
        "tracks": '[{"video_id": "vX", "title": "T", "artist": "A"}]',
        "recipe": '{"model": "mode", "mode_id": 2}'})
    assert r.status_code == 200
    assert completed_generated_playlist.call_args.kwargs['recipe'] == {'model': 'mode', 'mode_id': 2}
    assert store.modes.pick_counts() == {2: 1}


def test_generate_no_pick_without_mode(store, completed_generated_playlist):
    _seed_modes(store)
    _client(store).post("/home/generate", data={
        "name": "M", "tracks": '[{"video_id": "vX", "title": "T", "artist": "A"}]',
        "recipe": 'null'})
    assert completed_generated_playlist.call_args.kwargs['recipe'] is None
    assert store.modes.pick_counts() == {}
