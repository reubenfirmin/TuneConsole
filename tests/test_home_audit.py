import json
import logging
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, seed_catalog_track
from tests.home_fixture_support import home_routes_only, without_metadata_providers
from tests.onboarding_support import publish_live_mix_inputs
from tests.store_fixture_support import bounded_consumer_schema, uncontended_file_consumer_store as store
from yt_playlist.core import logsetup
from yt_playlist.rec import home_audit
from yt_playlist.rec.surfaces import ForYouItem
from yt_playlist.rec import home_cards as home
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'native_sql_calls')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'), ('britpop', 'Britpop'), ('techno', 'Techno'),
                                  ('house', 'House')), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def audit_path(tmp_path):
    logger = logging.getLogger("yt_playlist.home_audit")
    saved = list(logger.handlers), logger.level, logger.propagate
    path = tmp_path / "home-cards.jsonl"
    logsetup.configure_home_audit(path)
    yield path
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in saved[0]:
        logger.addHandler(handler)
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _client(store):
    identity = store.accounts.upsert_account('main', None, True)
    app = create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: 1000,
                     background_work=False)
    return TestClient(app, base_url='http://127.0.0.1')


def test_audit_snapshots_actual_genres_and_decades_with_unknown_coverage(store, audit_path):
    tid = store.tracks.upsert_track("v1", "Owned", "Artist", None, None)
    store.tracks.set_track_year(tid, "1995")
    tracks = [ForYouItem("Owned", "Artist", "", "v1", None, 0, "", key="owned|artist", genre="Jazz"),
              {"key": "new|a", "genre": "Britpop", "year": 2001},
              {"key": "unknown|b", "artist": "Unknown"}]
    proto = home._proto(store, "explore", "From your catalog", tracks, 1000)
    proto["recipe"] = {"facets": {"genres": ["techno"]}}  # intent must not masquerade as actual content
    home_audit.record(store, [proto], 1000, source="/home/cards", epoch=8, card_epochs={"explore": 3})
    store.tracks.set_track_year(tid, "2020")  # later enrichment cannot alter the written snapshot
    row = _rows(audit_path)[0]
    card = row["cards"][0]
    assert row["timestamp"] == "1970-01-01T00:16:40+00:00"
    assert card["framing"] == "explore" and card["epoch"] == 3
    assert card["description"] == proto["note"]
    assert card["genres"] == {"jazz": 1, "britpop": 1}
    assert card["decades"] == {"1990": 1, "2000": 1}
    assert card["unknown_genre_count"] == card["unknown_decade_count"] == 1
    assert 'track_keys' not in card and 'recipe' not in card


def test_routes_audit_mode_offers_previews_and_refreshes(
        store, audit_path, monkeypatch, home_routes_only, without_metadata_providers):
    from yt_playlist.rec import home_prepared

    # This consumer owns HTTP audit capture, not candidate preparation. Supply
    # completed offers while retaining actual metadata, epochs and audit files.
    offers = []
    modes = []
    for mode_id, lane in enumerate(('wheelhouse', 'explore', 'fresh'), start=1):
        key = f'audit {mode_id}|artist'
        seed_catalog_track(store, key, f'Audit {mode_id}', 'Artist', video_id=f'audit-{mode_id}', genre='Jazz')
        proto = home._proto(store, lane, f'A {lane} mix', [
            {'key': key, 'title': f'Audit {mode_id}', 'artist': 'Artist',
             'video_id': f'audit-{mode_id}', 'genre': 'Jazz'}], 1000)
        proto.update(mode_id=None, recipe={'lane': lane}, experiment_id=home.experiment_id(proto))
        if lane != 'fresh':
            proto['mode_id'] = mode_id
            modes.append({'mode_id': mode_id, 'label': lane, 'families': [('jazz', 1)],
                          'centroid': [1.0], 'size': 1, 'rep_keys': [key]})
        offers.append(proto)
    store.modes.replace_modes(modes, retired_ids=[], now=1000)

    def completed(reader):
        protos = deepcopy(offers)
        return home.CardsResult(protos, [{'proto': p, 'pool': None} for p in protos],
                                None, None, [], [], home._epoch(reader, 'cards'), None)

    monkeypatch.setattr(home_prepared, 'load', lambda *args, **kwargs: {})
    monkeypatch.setattr(home_prepared, 'peek', lambda *args, **kwargs: {})
    monkeypatch.setattr(home_prepared, 'cards', lambda reader, *args, **kwargs: completed(reader))
    monkeypatch.setattr(home.HomeCards, 'cards', lambda service, request: completed(service.store))
    monkeypatch.setattr(home_prepared, 'remember', lambda *args, **kwargs: None)
    with _client(store) as client:
        assert client.get("/home/cards").status_code == 200
        assert client.get("/home/cards").status_code == 200
        assert client.post("/home/breadth", data={"breadth_bias": "0.5"}).status_code == 200
        assert client.post("/home/refresh-cards").status_code == 200
    rows = _rows(audit_path)
    assert [r["source"] for r in rows] == ["/home/cards", "/home/cards", "/home/breadth", "/home/refresh-cards"]
    assert [r["menu_epoch"] for r in rows] == [0, 0, 0, 1]
    assert rows[0]["cards"] == rows[1]["cards"]
    for row in rows:
        cards = {c["framing"]: c for c in row["cards"]}
        assert set(cards) == {"wheelhouse", "explore", "fresh"}
        for mode_id, lane in enumerate(("wheelhouse", "explore"), start=1):
            card = cards[lane]
            assert card["mode_id"] == mode_id
            assert 'recipe' not in card and 'track_keys' not in card
            assert card["epoch"] == row["menu_epoch"]
        # Fresh comes from the shared pool and keeps its own epoch across menu refreshes.
        assert cards["fresh"]["mode_id"] is None
        assert 'recipe' not in cards['fresh']
        assert cards["fresh"]["epoch"] == 0
        assert all(c["genres"] == {"jazz": 1} for c in cards.values())


def test_single_card_refresh_is_audited_and_audit_failure_does_not_break_home(
        store, audit_path, monkeypatch, home_routes_only, without_metadata_providers, caplog):
    tracks = publish_live_mix_inputs(store)
    track = next(track for track in tracks if track['video_id'].startswith('live-wheelhouse-'))
    with _client(store) as client:
        assert client.post("/home/refresh-card/wheelhouse").status_code == 200
        row = _rows(audit_path)[0]
        assert row["source"] == "/home/refresh-card/wheelhouse"
        assert row["cards"][0]["framing"] == 'wheelhouse'
        assert row["cards"][0]["epoch"] == 1

        def fail(*args, **kwargs):
            raise OSError("audit unavailable")
        monkeypatch.setattr(home_audit, "record", fail)
        response = client.get("/home/cards")
        assert response.status_code == 200
        assert f'data-title="{track["title"]}"' in response.text
        assert 'audit unavailable' in caplog.text


def test_audit_appends_across_reconfigure_and_rotates_separately(store, audit_path):
    def record(now):
        home_audit.record(store, [], now, source="/home/cards", epoch=0, card_epochs={})
    record(1000)
    logsetup.configure_home_audit(audit_path)
    record(1001)
    assert len(_rows(audit_path)) == 2
    handler = logging.getLogger("yt_playlist.home_audit").handlers[0]
    assert handler.backupCount == 30
    handler.doRollover()
    record(1002)
    assert len(_rows(audit_path)) == 1
    archives = list(audit_path.parent.glob("home-cards.jsonl.*"))
    assert len(archives) == 1 and len(_rows(archives[0])) == 2
