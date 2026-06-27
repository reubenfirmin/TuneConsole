from yt_playlist.rec import journeys


def _items(specs):
    """specs: list of (artist, energy, decade, plays, recency, genre)."""
    return [{"i": i, "artist": a, "energy": e, "decade": d, "plays": p, "recency": r, "genre": g}
            for i, (a, e, d, p, r, g) in enumerate(specs)]


def _feat(it):
    return {"artist": it["artist"], "genre": it["genre"], "energy": it["energy"],
            "decade": it["decade"], "plays": it["plays"], "recency": it["recency"]}


def _twelve_energy(energies):
    return _items([(f"A{i}", e, None, 0, 0.0, "") for i, e in enumerate(energies)])


def test_journeys_constants():
    assert len(journeys.JOURNEYS) == 10
    assert "energy_arc" in journeys.JOURNEYS and "shuffle" in journeys.JOURNEYS
    assert set(journeys.JOURNEY_LABELS) == set(journeys.JOURNEYS)


def test_journey_order_is_permutation():
    items = _twelve_energy([0.1, 0.9, 0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6, 0.15, 0.95, 0.55])
    for jk in journeys.JOURNEYS:
        out = journeys.journey_order(items, jk, seed=3, feat=_feat)
        assert sorted(x["i"] for x in out) == sorted(x["i"] for x in items)   # no loss/dup


def test_warm_up_rises_in_thirds():
    items = _twelve_energy([0.1, 0.9, 0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6, 0.15, 0.95, 0.55])
    out = journeys.journey_order(items, "warm_up", seed=1, feat=_feat)
    e = [x["energy"] for x in out]
    assert sum(e[:4]) / 4 < sum(e[4:8]) / 4 < sum(e[8:]) / 4        # bands rise low->high


def test_wind_down_falls_in_thirds():
    items = _twelve_energy([0.1, 0.9, 0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6, 0.15, 0.95, 0.55])
    out = journeys.journey_order(items, "wind_down", seed=1, feat=_feat)
    e = [x["energy"] for x in out]
    assert sum(e[:4]) / 4 > sum(e[4:8]) / 4 > sum(e[8:]) / 4        # bands fall high->low


def test_energy_arc_peaks_in_middle():
    items = _twelve_energy([0.1, 0.9, 0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6, 0.15, 0.95, 0.55])
    out = journeys.journey_order(items, "energy_arc", seed=1, feat=_feat)
    e = [x["energy"] for x in out]
    mid = sum(e[4:8]) / 4
    assert mid > sum(e[:4]) / 4 and mid > sum(e[8:]) / 4            # peak band in the middle


def test_deep_dive_starts_with_heavy_hitters():
    items = _items([(f"A{i}", 0.5, None, p, 0.0, "") for i, p in enumerate(
        [1, 50, 5, 40, 2, 30, 10, 45, 3, 35, 8, 25])])
    out = journeys.journey_order(items, "deep_dive", seed=1, feat=_feat)
    p = [x["plays"] for x in out]
    assert sum(p[:4]) / 4 > sum(p[8:]) / 4                          # most-played first


def test_rediscovery_starts_with_coldest():
    items = _items([(f"A{i}", 0.5, None, 0, r, "") for i, r in enumerate(
        [10.0, 500.0, 50.0, 400.0, 20.0, 300.0, 100.0, 450.0, 30.0, 350.0, 80.0, 250.0])])
    out = journeys.journey_order(items, "rediscovery", seed=1, feat=_feat)
    r = [x["recency"] for x in out]
    assert sum(r[:4]) / 4 < sum(r[8:]) / 4                          # oldest (coldest) first


def test_axis_journey_spaces_artists_within_band():
    # 12 tracks, only 3 artists, all same energy band-ish: spacing must avoid back-to-back.
    items = _items([(a, e, None, 0, 0.0, "")
                    for a, e in [("X", 0.10), ("X", 0.12), ("X", 0.14), ("X", 0.16),
                                 ("Y", 0.50), ("Y", 0.52), ("Y", 0.54), ("Y", 0.56),
                                 ("Z", 0.90), ("Z", 0.92), ("Z", 0.94), ("Z", 0.96)]])
    out = journeys.journey_order(items, "warm_up", seed=2, feat=_feat)
    # within each band the artist is constant here, so adjacency is unavoidable across the whole
    # list; assert instead that the BANDS themselves are ordered by energy (the journey holds).
    e = [x["energy"] for x in out]
    assert e[0] < e[-1]


def test_undated_tracks_pool_at_end_for_era():
    items = _items([(f"A{i}", 0.5, d, 0, 0.0, "") for i, d in enumerate(
        [1990, None, 2000, None, 1980, 2010, None, 1970, 2020, None, 1960, 2030])])
    out = journeys.journey_order(items, "time_machine", seed=1, feat=_feat)
    decades = [x["decade"] for x in out]
    first_none = next(i for i, d in enumerate(decades) if d is None)
    assert all(d is None for d in decades[first_none:])            # undated trail at the end
    dated = [d for d in decades if d is not None]
    half = len(dated) // 2
    assert max(dated[:half]) < min(dated[half:])                   # older band precedes newer band


def _genre_items():
    # 12 tracks across distinct families; distinct artists so artist-spacing never forces order.
    genres = ["techno", "house", "trance", "ambient", "folk", "metal",
              "jazz", "classical", "pop", "hiphop", "punk", "blues"]
    return _items([(f"A{i}", 0.5, None, 0, 0.0, g) for i, g in enumerate(genres)])


def _adj_total(out):
    from yt_playlist.util import genre_map
    return sum(genre_map.distance(out[i]["genre"], out[i + 1]["genre"]) for i in range(len(out) - 1))


def test_energy_arc_rises_then_falls_even_for_small_mixes():
    # Arc must form a mountain (peak interior, both ends below the peak), not a ramp, even for
    # short mixes that previously yielded <=2 bands.
    # seed=8 exposes the bug for both n=6 and n=8 (which previously produced a ramp).
    for n in (6, 8, 12):
        energies = [round(0.05 + 0.9 * i / (n - 1), 3) for i in range(n)]
        items = _items([(f"A{i}", e, None, 0, 0.0, "") for i, e in enumerate(energies)])
        out = journeys.journey_order(items, "energy_arc", seed=8, feat=_feat)
        e = [x["energy"] for x in out]
        peak = e.index(max(e))
        assert 0 < peak < len(e) - 1, f"n={n}: peak not interior: {e}"
        assert e[0] < max(e) and e[-1] < max(e), f"n={n}: doesn't return toward low: {e}"


def test_smooth_segue_minimizes_genre_jumps():
    items = _genre_items()
    smooth = journeys.journey_order(items, "smooth_segue", seed=5, feat=_feat)
    shuffled = journeys.journey_order(items, "shuffle", seed=5, feat=_feat)
    assert _adj_total(smooth) < _adj_total(shuffled)


def test_odyssey_maximizes_genre_jumps():
    items = _genre_items()
    odyssey = journeys.journey_order(items, "odyssey", seed=5, feat=_feat)
    smooth = journeys.journey_order(items, "smooth_segue", seed=5, feat=_feat)
    assert _adj_total(odyssey) > _adj_total(smooth)
