"""Task 5 (#50): populate_radio_tracks persists unowned YTM-radio tracks into the discovered pool
(deduped vs owned and the existing pool), so the cold ranker and Clusters share one cold source."""
from types import SimpleNamespace

from yt_playlist.rec import discover
from yt_playlist.util.matching import identity_key


class _Client:
    def get_watch_playlist(self, vid):
        return {"tracks": [
            {"videoId": "new1", "title": "New Song", "artists": [{"name": "New Artist"}]},
            {"videoId": "own1", "title": "Owned", "artists": [{"name": "Owned Artist"}]}]}


def _ctx(store):
    return SimpleNamespace(store=store, client_provider=lambda: {"yt": _Client()},
                           now_fn=lambda: 1000.0,
                           logger=SimpleNamespace(info=lambda *a, **k: None))


def _fake_rec_dao(owned):
    return lambda store: SimpleNamespace(library_keys=lambda: set(owned))


def test_radio_pull_persists_unowned_and_dedups(monkeypatch, store):
    owned = identity_key("Owned", "Owned Artist")
    monkeypatch.setattr(discover, "RecDao", _fake_rec_dao({owned}))
    monkeypatch.setattr(store, "top_tracks", lambda n: [{"video_id": "seed1"}], raising=False)

    n = discover.populate_radio_tracks(_ctx(store), 1000.0)
    pooled = {r["identity_key"] for r in store.get_discovered_tracks()}
    assert identity_key("New Song", "New Artist") in pooled
    assert owned not in pooled
    assert n == 1

    # Second pass: the new track is now in the pool, so it is not re-added.
    assert discover.populate_radio_tracks(_ctx(store), 1001.0) == 0


def test_no_client_returns_zero(store):
    # No client short-circuits to 0 before any DB work.
    ctx = SimpleNamespace(store=store, client_provider=lambda: {},
                          now_fn=lambda: 1.0, logger=SimpleNamespace(info=lambda *a, **k: None))
    assert discover.populate_radio_tracks(ctx, 1.0) == 0
