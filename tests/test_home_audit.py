import json
import logging

import pytest

from yt_playlist.core import logsetup
from yt_playlist.rec import home_audit
from yt_playlist.rec.surfaces import ForYouItem
from yt_playlist.web.routes import home


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


def test_audit_snapshots_actual_genres_and_decades_with_unknown_coverage(store, audit_path):
    tid = store.upsert_track("v1", "Owned", "Artist", None, None)
    store.set_track_year(tid, "1995")
    tracks = [ForYouItem("Owned", "Artist", "", "v1", None, 0, "", key="owned|artist", genre="Jazz"),
              {"key": "new|a", "genre": "Britpop", "year": 2001},
              {"key": "unknown|b", "artist": "Unknown"}]
    proto = home._proto(store, "explore", "From your catalog", tracks, 1000)
    proto["recipe"] = {"facets": {"genres": ["techno"]}}  # intent must not masquerade as actual content
    home_audit.record(store, [proto], 1000, source="/home/cards", epoch=8, card_epochs={"explore": 3})
    store.set_track_year(tid, "2020")  # later enrichment cannot alter the written snapshot
    row = _rows(audit_path)[0]
    card = row["cards"][0]
    assert row["timestamp"] == "1970-01-01T00:16:40+00:00"
    assert card["framing"] == "explore" and card["epoch"] == 3
    assert card["description"] == proto["note"]
    assert card["genres"] == {"jazz": 1, "britpop": 1}
    assert card["decades"] == {"1990": 1, "2000": 1}
    assert card["unknown_genre_count"] == card["unknown_decade_count"] == 1
    assert card["track_keys"] == ["owned|artist", "new|a", "unknown|b"]


def test_routes_audit_mode_offers_previews_and_refreshes(store, audit_path):
    from tests.test_mode_cards_route import _seed_bundles, _client
    _seed_bundles(store)
    client = _client(store)
    assert client.get("/home/cards").status_code == 200
    assert client.get("/home/cards").status_code == 200
    assert client.post("/home/breadth", data={"breadth_bias": "0.5"}).status_code == 200
    assert client.post("/home/refresh-cards").status_code == 200
    rows = _rows(audit_path)
    assert [r["source"] for r in rows] == ["/home/cards", "/home/cards", "/home/breadth", "/home/refresh-cards"]
    assert [r["menu_epoch"] for r in rows] == [0, 0, 0, 1]
    assert rows[0]["cards"] == rows[1]["cards"]
    assert all(c["mode_id"] is not None and c["genres"] for r in rows for c in r["cards"])


def test_single_card_refresh_is_audited_and_audit_failure_does_not_break_home(store, audit_path, monkeypatch):
    from tests.test_mode_cards_route import _client
    proto = home._proto(store, "wheelhouse", "More in your wheelhouse",
                        [{"key": "a|b", "title": "A", "artist": "B", "genre": "Jazz"}], 1000)
    monkeypatch.setattr(home, "_one_card", lambda *_: proto)
    client = _client(store)
    assert client.post("/home/refresh-card/wheelhouse").status_code == 200
    row = _rows(audit_path)[0]
    assert row["source"] == "/home/refresh-card/wheelhouse"
    assert row["cards"][0]["epoch"] == 1

    def fail(*args, **kwargs):
        raise OSError("audit unavailable")
    monkeypatch.setattr(home_audit, "record", fail)
    response = client.get("/home/cards")
    assert response.status_code == 200 and "More in your wheelhouse" in response.text


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
