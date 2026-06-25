"""Enrichment provider config: load merges/repairs, save validates."""
import json

import pytest

from yt_playlist.providers import enrichment as E


def names(cfg):
    return [p["name"] for p in cfg]


def test_load_seeds_default_order_all_enabled(store):
    cfg = E.load_config(store)
    assert names(cfg) == [p["name"] for p in E.PROVIDERS]
    assert all(p["enabled"] for p in cfg)


def test_load_drops_unknown_and_appends_missing(store):
    # Saved state knows only deezer (+ a bogus name); the rest must be appended, enabled.
    store.set_setting("enrichment_order", json.dumps(
        [{"name": "bogus", "enabled": True}, {"name": "deezer", "enabled": False}]))
    cfg = E.load_config(store)
    assert "bogus" not in names(cfg)
    assert names(cfg)[0] == "deezer"                       # saved order preserved
    assert set(names(cfg)) == {p["name"] for p in E.PROVIDERS}
    assert next(p for p in cfg if p["name"] == "deezer")["enabled"] is False
    assert next(p for p in cfg if p["name"] == "musicbrainz")["enabled"] is True


def test_load_repairs_acousticbrainz_after_musicbrainz(store):
    store.set_setting("enrichment_order", json.dumps(
        [{"name": "acousticbrainz", "enabled": True}, {"name": "musicbrainz", "enabled": True}]))
    cfg = names(E.load_config(store))
    assert cfg.index("acousticbrainz") > cfg.index("musicbrainz")


def test_load_tolerates_corrupt_json(store):
    store.set_setting("enrichment_order", "{not json")
    assert names(E.load_config(store)) == [p["name"] for p in E.PROVIDERS]


def test_save_rejects_acousticbrainz_before_musicbrainz(store):
    with pytest.raises(ValueError):
        E.save_config(store, [{"name": "acousticbrainz", "enabled": True},
                              {"name": "musicbrainz", "enabled": True}])


def test_save_rejects_unknown_name(store):
    with pytest.raises(ValueError):
        E.save_config(store, [{"name": "nope", "enabled": True}])


def test_save_roundtrips_reorder_and_disabled(store):
    E.save_config(store, [
        {"name": "lastfm", "enabled": False},
        {"name": "deezer", "enabled": True},
        {"name": "musicbrainz", "enabled": True},
        {"name": "acousticbrainz", "enabled": False},
        {"name": "discogs", "enabled": True},
    ])
    cfg = E.load_config(store)
    assert names(cfg)[:3] == ["lastfm", "deezer", "musicbrainz"]
    assert next(p for p in cfg if p["name"] == "lastfm")["enabled"] is False
    assert next(p for p in cfg if p["name"] == "acousticbrainz")["enabled"] is False


# ── web: the Enrichment tab renders and persists ──────────────────────────────────────
from fastapi.testclient import TestClient                 # noqa: E402
from yt_playlist.web.app import create_app                # noqa: E402
from tests.conftest import FakeClient                     # noqa: E402


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def test_setup_renders_enrichment_tab(store):
    html = _client(store).get("/setup").text
    assert ">Enrichment<" in html                         # the tab button
    for p in E.PROVIDERS:                                 # every provider card
        assert p["label"] in html
    assert "last.fm/api" in html                          # inline key UI


def test_setup_enrichment_persists_reorder_and_disable(store):
    c = _client(store)
    r = c.post("/setup/enrichment", data={
        "order": ["deezer", "musicbrainz", "acousticbrainz", "lastfm", "discogs"],
        "enabled": ["deezer", "musicbrainz", "acousticbrainz", "discogs"]})  # lastfm unchecked
    assert r.status_code == 200
    cfg = E.load_config(store)
    assert names(cfg)[0] == "deezer"
    assert next(p for p in cfg if p["name"] == "lastfm")["enabled"] is False


def test_setup_enrichment_invalid_order_no_500(store):
    # AcousticBrainz before MusicBrainz: save_config rejects; route swallows it, returns last-good.
    c = _client(store)
    r = c.post("/setup/enrichment", data={
        "order": ["acousticbrainz", "musicbrainz"],
        "enabled": ["acousticbrainz", "musicbrainz"]})
    assert r.status_code == 200
    cfg = names(E.load_config(store))
    assert cfg.index("acousticbrainz") > cfg.index("musicbrainz")
