"""Road Trip journey ordering and source interleaving."""
from yt_playlist.rec import journeys
from tests.road_trip_support import no_network

def _rt_item(vid, artist, source):
    return {"video_id": vid, "artist": artist, "genre": "", "source": source}


def test_road_trip_journey_interleaves_by_source_ratio():
    mine = [_rt_item(f"m{i}", f"Mine Artist {i}", "mine") for i in range(12)]
    theirs = [_rt_item(f"t{i}", f"Their Artist {i}", "theirs") for i in range(4)]
    items = mine + theirs

    def feat(it):
        return {"artist": it["artist"], "genre": it["genre"], "source": it["source"]}

    ordered = journeys.journey_order(items, "road_trip", seed=1, feat=feat)

    assert len(ordered) == len(items)
    assert {it["video_id"] for it in ordered} == {it["video_id"] for it in items}
    # A blocked arrangement (all mine, then all theirs) is exactly 2 runs (1 source-switch).
    # Interleaving 4 "theirs" tracks one-per-band across 16 (see _road_trip_order's banding) can
    # never collapse to that: each band has >=1 "mine" alongside its "theirs", so every band-local
    # theirs is bordered by mine on at least one side. Assert well clear of the blocked case.
    sources = [it["source"] for it in ordered]
    runs = sum(1 for a, b in zip(sources, sources[1:]) if a != b) + 1
    assert runs >= 4


def test_road_trip_journey_dispatches_to_dedicated_ordering(monkeypatch):
    # The two behavioral tests above use loose, shuffle-robust assertions (deliberately, to avoid
    # flakiness across seeds) - loose enough that journey_order's existing generic _space(...)
    # fallback can satisfy them by chance. This test exists so a regression that silently drops the
    # "road_trip" dispatch branch (falling back to that generic path) still gets caught.
    called = {}

    def fake_road_trip_order(items, seed, feat):
        called["hit"] = True
        return list(items)

    monkeypatch.setattr(journeys, "_road_trip_order", fake_road_trip_order)
    items = [_rt_item(f"m{i}", f"Artist {i}", "mine") for i in range(5)]

    journeys.journey_order(items, "road_trip", seed=1,
                           feat=lambda it: {"artist": it["artist"], "genre": it["genre"],
                                            "source": it["source"]})

    assert called.get("hit") is True


def test_road_trip_journey_avoids_back_to_back_same_artist():
    # 2 same-artist "mine" + 2 distinct-artist "theirs": full separation is feasible (unlike e.g.
    # 3 same-artist tracks among 4 total, where pigeonhole makes some adjacency unavoidable).
    items = [_rt_item("m0", "Same Artist", "mine"), _rt_item("m1", "Same Artist", "mine"),
            _rt_item("t0", "Other Artist One", "theirs"), _rt_item("t1", "Other Artist Two", "theirs")]

    def feat(it):
        return {"artist": it["artist"], "genre": it["genre"], "source": it["source"]}

    ordered = journeys.journey_order(items, "road_trip", seed=2, feat=feat)

    artists_in_order = [it["artist"] for it in ordered]
    back_to_back = any(a == b for a, b in zip(artists_in_order, artists_in_order[1:]))
    assert not back_to_back
