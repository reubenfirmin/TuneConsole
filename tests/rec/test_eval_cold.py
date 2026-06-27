"""Task 7 (#50): eval_recs.cold_rankable reports how many discovered-pool tracks the cold ranker can
actually score vs the pool size (the issue's measured 'cold tracks become rankable' criterion)."""
from yt_playlist.rec import eval_recs, surfaces
from yt_playlist.rec.surfaces import ForYouItem


def test_cold_rankable_counts_ranked_pool(monkeypatch, store):
    monkeypatch.setattr(store, "get_discovered_tracks",
                        lambda: [{"identity_key": "a|x"}, {"identity_key": "b|y"}], raising=False)
    monkeypatch.setattr(surfaces, "cold_candidates",
                        lambda s, now, **k: [ForYouItem("T", "A", "", "v", None, 0, "r", "a|x", lane="cold")])
    assert eval_recs.cold_rankable(store, 0.0) == {"rankable": 1, "pool": 2}


def test_cold_rankable_zero_when_unrankable(monkeypatch, store):
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: [{"identity_key": "a|x"}], raising=False)
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, now, **k: [])
    assert eval_recs.cold_rankable(store, 0.0) == {"rankable": 0, "pool": 1}
