"""The DJ ordering model: seeded shuffle -> anti-artist-repeat -> stickiness-scaled genre smoothing."""
from yt_playlist.rec import genre_map, recommend
from yt_playlist.rec.recommend import ForYouItem
from yt_playlist.util.matching import identity_key


def _t(artist, genre, i):
    return {"artist": artist, "genre": genre, "title": f"{artist}-{genre}-{i}"}


def _fy(artist, i):
    return ForYouItem(f"S-{artist}-{i}", artist, "", f"v-{artist}-{i}", None, 5, "r",
                      identity_key(f"S-{artist}-{i}", artist), "comfort")


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


def test_dj_order_works_on_foryou_items():
    # ForYouItems are dataclasses, not dicts — dj_order must still read .artist/.genre off them, or
    # generated playlists (built from ForYouItems) come out artist-clustered. The 'comfort' bug.
    items = ([_fy("Hermanos", i) for i in range(4)] + [_fy("Younger", i) for i in range(3)]
             + [_fy("Ritmo", i) for i in range(2)] + [_fy("Solo", 0)])
    for it in items:
        it.genre = "Latin" if it.artist == "Hermanos" else "Electronica"
    out = recommend.dj_order(items, stickiness=0.0, seed=1)
    adj_same = sum(1 for a, b in zip(out, out[1:]) if a.artist == b.artist)
    assert adj_same == 0                                              # artists spaced, not clustered
    assert sorted(i.video_id for i in out) == sorted(i.video_id for i in items)   # permutation


def test_attach_genres_fills_from_store(store):
    a = store.upsert_track("v1", "S1", "Hermanos", "", None, 1); store.set_track_genre(a, "Latin")
    store.upsert_track("v2", "S2", "Ritmo", "", None, 1)             # left untagged on purpose
    fy = ForYouItem("S1", "Hermanos", "", "v1", None, 5, "r", identity_key("S1", "Hermanos"), "comfort")
    d = {"video_id": "v2", "title": "S2", "artist": "Ritmo"}         # DOM-style dict, no key
    recommend.attach_genres(store, [fy, d])
    assert fy.genre == "Latin"          # ForYouItem gets its genre from the library (by identity_key)
    assert d["genre"] == ""             # dict path works too; untagged -> empty (no crash, no journey)
