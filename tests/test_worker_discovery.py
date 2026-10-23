from fastapi.testclient import TestClient
import pytest

from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from yt_playlist.web.context import Ctx
from tests.conftest import FakeClient
from yt_playlist.rec import home_prepared


class _ArtistClient(FakeClient):
    """A client that answers the outward-discovery fetch with one new + one owned album."""
    def search(self, query, filter="songs"):
        return [{"browseId": "ART1"}]

    def get_artist(self, browse_id):
        return {"name": "Fav", "description": None, "thumbnails": None, "subscribers": None,
                "albums": {"results": [
                    {"title": "Brand New LP", "year": "2024", "browseId": "ALB_NEW", "thumbnails": None},
                    {"title": "Owned Record", "year": "2010", "browseId": "ALB_OLD", "thumbnails": None},
                ]}}


def _ctx(store, client):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return iid, Ctx(store=store, client_provider=lambda: {iid: client}, now_fn=lambda: 1.0,
                    templates=None, jobs=None)


def test_worker_materializes_proposals(store, monkeypatch):
    iid, ctx = _ctx(store, _ArtistClient())
    t = store.tracks.upsert_track("v1", "Song", "Fav", None, None)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "P", "P", 1, "h", 0.0), [t])
    store.history.record_history_plays(iid, 1.0, ["song|fav"], source_timezone="UTC")

    worker = RecWorker(ctx)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    worker._discover(1.0)
    worker.shutdown()
    # discovery now accumulates into the pool (scan-ledger backed), not the overwrite proposals
    titles = {a["title"] for a in store.discovery.get_discovered_albums()}
    assert "Brand New LP" in titles        # the new album was scanned into the pool


def test_home_discover_serves_cached(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.discovery.upsert_discovered_album("B", "X", "Cached LP", "2024", None, now=1.0)
    assert home_prepared.prepare(store, 1.0) is not None
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    assert "Cached LP" in c.get("/home/discover").text     # served from materialized proposals, no fetch


def test_discovery_pass_publishes_new_songs_before_the_next_rebuild(store, monkeypatch):
    from yt_playlist.rec import discover, fresh_pool

    _, ctx = _ctx(store, FakeClient())
    monkeypatch.setattr(discover, "new_artists", lambda ctx: [])

    def populate(ctx, now):
        for i in range(12):
            store.discovery.upsert_discovered_track(f"new {i}|artist {i}", f"v{i}", f"New {i}",
                f"Artist {i}", None, None, "Jazz", "2020", "radio:seed", now)

    monkeypatch.setattr(discover, "populate_discovered_tracks", populate)
    monkeypatch.setattr(discover, "populate_radio_tracks", lambda *a: None)
    RecWorker(ctx)._discover(10)
    assert len(fresh_pool.pick(store, 11)) == 12


@pytest.mark.parametrize("fails", [False, True])
def test_discovery_finishes_and_queues_home_refresh_only_on_success(store, monkeypatch, fails):
    from yt_playlist.rec import discover
    _, ctx = _ctx(store, FakeClient())
    worker = RecWorker(ctx)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)

    def scan(*args):
        assert worker.discovery_busy and not worker.busy
        if fails:
            raise RuntimeError("Offline")
        return {}

    monkeypatch.setattr(discover, "run_discovery", scan)
    if fails:
        with pytest.raises(RuntimeError, match="Offline"):
            worker._discover(10)
    else:
        worker._discover(10)
    assert not worker._discovering
    assert worker.jobs.has('home') is (not fails)
    assert worker.discovery_busy is (not fails)
    worker.shutdown()


