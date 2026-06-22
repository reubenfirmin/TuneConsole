"""The DJ ordering model: seeded shuffle -> anti-artist-repeat -> stickiness-scaled genre smoothing."""
from yt_playlist import genre_map, recommend


def _t(artist, genre, i):
    return {"artist": artist, "genre": genre, "title": f"{artist}-{genre}-{i}"}


def test_dj_order_is_permutation_and_deterministic():
    tracks = [_t(f"A{i}", "Techno", i) for i in range(4)] + [_t(f"B{i}", "Folk", i) for i in range(4)]
    o1 = recommend.dj_order(tracks, stickiness=0.5, seed=7)
    o2 = recommend.dj_order(tracks, stickiness=0.5, seed=7)
    assert [t["title"] for t in o1] == [t["title"] for t in o2]                 # deterministic
    assert sorted(t["title"] for t in o1) == sorted(t["title"] for t in tracks)  # same multiset


def test_dj_order_avoids_back_to_back_same_artist():
    tracks = [_t("Repeat", "Techno", i) for i in range(5)] + [_t("Other", "Techno", i) for i in range(5)]
    out = recommend.dj_order(tracks, stickiness=0.0, seed=1)
    adj_same = sum(1 for a, b in zip(out, out[1:]) if a["artist"] == b["artist"])
    assert adj_same == 0


def test_dj_order_stickiness_smooths_genre_transitions():
    tracks = [_t(f"T{i}", "Techno", i) for i in range(4)] + [_t(f"F{i}", "Folk", i) for i in range(4)]

    def adj_dist(o):
        return sum(genre_map.distance(a["genre"], b["genre"]) for a, b in zip(o, o[1:]))

    smooth = recommend.dj_order(tracks, stickiness=1.0, seed=3)
    shuffled = recommend.dj_order(tracks, stickiness=0.0, seed=3)
    assert adj_dist(smooth) < adj_dist(shuffled)     # high stickiness = smoother genre segues
