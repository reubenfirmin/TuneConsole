from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home import lastfm_nudge_due
from yt_playlist.providers import lastfm
from tests.conftest import FakeClient


def _seed_sparse(store, genre, processed):
    # Insert `processed` tracks, `genre` of which have a genre, so coverage = genre/processed.
    for i in range(processed):
        store.conn.execute(
            "INSERT INTO tracks(id, identity_key, title, artist, first_enriched_at, genre) "
            "VALUES (?,?,?,?,?,?)",
            (i, f"k{i}", f"T{i}", "A", 1.0, "rock" if i < genre else None))
    store.conn.commit()


def test_due_when_sparse_no_key_not_dismissed(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    _seed_sparse(store, genre=5, processed=100)
    assert lastfm_nudge_due(store) is True


def test_not_due_when_key_present(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: True)
    _seed_sparse(store, genre=5, processed=100)
    assert lastfm_nudge_due(store) is False


def test_not_due_when_coverage_high(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    _seed_sparse(store, genre=95, processed=100)
    assert lastfm_nudge_due(store) is False


def test_not_due_when_dismissed(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    _seed_sparse(store, genre=5, processed=100)
    store.set_setting("lastfm_nudge_dismissed", "1")
    assert lastfm_nudge_due(store) is False


def test_not_due_when_nothing_processed(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    assert lastfm_nudge_due(store) is False


def test_banner_renders_and_dismiss_persists(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    _seed_sparse(store, genre=5, processed=100)
    store.set_setting("last_sync_at", "1700000000")
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "lastfm-nudge" in c.get("/").text
    assert c.post("/onboard/lastfm/dismiss").status_code == 200
    assert store.get_setting("lastfm_nudge_dismissed") == "1"
    assert "lastfm-nudge" not in c.get("/").text
