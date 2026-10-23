"""The DJ ordering model: seeded shuffle -> anti-artist-repeat -> stickiness-scaled genre smoothing."""
import pytest

from yt_playlist.util import genre_map
from yt_playlist.rec import ordering
from yt_playlist.rec.surfaces import ForYouItem
from yt_playlist.util.matching import identity_key


def _t(artist, genre, i):
    return {"artist": artist, "genre": genre, "title": f"{artist}-{genre}-{i}"}


def _fy(artist, i):
    return ForYouItem(f"S-{artist}-{i}", artist, "", f"v-{artist}-{i}", None, 5, "r",
                      identity_key(f"S-{artist}-{i}", artist), "comfort")


def test_dj_order_is_permutation_and_deterministic():
    tracks = [_t(f"A{i}", "Techno", i) for i in range(4)] + [_t(f"B{i}", "Folk", i) for i in range(4)]
    o1 = ordering.dj_order(tracks, stickiness=0.5, seed=7)
    o2 = ordering.dj_order(tracks, stickiness=0.5, seed=7)
    assert [t["title"] for t in o1] == [t["title"] for t in o2]                 # deterministic
    assert sorted(t["title"] for t in o1) == sorted(t["title"] for t in tracks)  # same multiset


def test_dj_order_avoids_back_to_back_same_artist():
    tracks = [_t("Repeat", "Techno", i) for i in range(5)] + [_t("Other", "Techno", i) for i in range(5)]
    out = ordering.dj_order(tracks, stickiness=0.0, seed=1)
    adj_same = sum(1 for a, b in zip(out, out[1:]) if a["artist"] == b["artist"])
    assert adj_same == 0


@pytest.mark.parametrize("counts", [(2, 1), (3, 1, 1), (4, 2, 1), (2, 2, 2), (5, 1, 1), (5,)])
@pytest.mark.parametrize("seed", range(12))
def test_dj_order_uses_all_available_artist_separators(counts, seed):
    tracks = [_t(str(artist), "Techno", i)
              for artist, count in enumerate(counts) for i in range(count)]
    out = ordering.dj_order(tracks, stickiness=0.4, seed=seed)
    # Each other artist can separate two songs from the dominant artist. Any
    # repeats beyond those separators are unavoidable, including single-artist sets.
    unavoidable = max(0, 2 * max(counts) - len(tracks) - 1)
    assert sum(a["artist"] == b["artist"] for a, b in zip(out, out[1:])) == unavoidable
    assert sorted(map(id, out)) == sorted(map(id, tracks))
    assert out == ordering.dj_order(tracks, stickiness=0.4, seed=seed)


def test_dj_order_stickiness_smooths_genre_transitions():
    tracks = [_t(f"T{i}", "Techno", i) for i in range(4)] + [_t(f"F{i}", "Folk", i) for i in range(4)]

    def adj_dist(o):
        return sum(genre_map.distance(a["genre"], b["genre"]) for a, b in zip(o, o[1:]))

    smooth = ordering.dj_order(tracks, stickiness=1.0, seed=3)
    shuffled = ordering.dj_order(tracks, stickiness=0.0, seed=3)
    assert adj_dist(smooth) < adj_dist(shuffled)     # high stickiness = smoother genre segues


def test_dj_order_works_on_foryou_items():
    # ForYouItems are dataclasses, not dicts: dj_order must still read .artist/.genre off them, or
    # generated playlists (built from ForYouItems) come out artist-clustered. The 'comfort' bug.
    items = ([_fy("Hermanos", i) for i in range(4)] + [_fy("Younger", i) for i in range(3)]
             + [_fy("Ritmo", i) for i in range(2)] + [_fy("Solo", 0)])
    for it in items:
        it.genre = "Latin" if it.artist == "Hermanos" else "Electronica"
    out = ordering.dj_order(items, stickiness=0.0, seed=1)
    adj_same = sum(1 for a, b in zip(out, out[1:]) if a.artist == b.artist)
    assert adj_same == 0                                              # artists spaced, not clustered
    assert sorted(i.video_id for i in out) == sorted(i.video_id for i in items)   # permutation


@pytest.mark.parametrize("size", [128, 512, 2048])
def test_dj_order_resolves_features_linearly_and_reuses_distances(monkeypatch, size):
    tracks = [_t(str(i % 8), "techno" if i % 2 else "folk", i) for i in range(size)]
    calls = {"artist": 0, "genre": 0, "family": 0, "distance": 0}
    read, family, distance = ordering._field, genre_map.family, genre_map.family_distance

    def field(item, name):
        calls[name] += 1
        return read(item, name)

    def resolve(genre):
        calls["family"] += 1
        return family(genre)

    def measure_distance(a, b):
        calls["distance"] += 1
        return distance(a, b)

    monkeypatch.setattr(ordering, "_field", field)
    monkeypatch.setattr(genre_map, "family", resolve)
    monkeypatch.setattr(genre_map, "family_distance", measure_distance)
    out = ordering.dj_order(tracks, stickiness=0.5, seed=11)
    assert calls["artist"] == calls["genre"] == calls["family"] == size
    assert calls["distance"] <= 4  # Two families, independent of the selection size.
    assert sorted(map(id, out)) == sorted(map(id, tracks))
    assert all(a["artist"] != b["artist"] for a, b in zip(out, out[1:]))


def test_dj_order_supports_opaque_keys_and_duplicate_occurrences():
    tracks = ["a", "a", "a", "b", "b", "c", "c"]
    calls = []

    def feat(key):
        calls.append(key)
        return {"artist": key, "genre": "techno"}

    out = ordering.dj_order(tracks, seed=4, feat=feat)
    assert calls == tracks  # One detached feature read per input occurrence.
    assert sorted(out) == sorted(tracks)
    assert all(a != b for a, b in zip(out, out[1:]))


def test_attach_genres_fills_from_store(store):
    a = store.tracks.upsert_track("v1", "S1", "Hermanos", "", None, 1); store.tracks.set_track_genre(a, "Latin")
    store.tracks.upsert_track("v2", "S2", "Ritmo", "", None, 1)             # left untagged on purpose
    fy = ForYouItem("S1", "Hermanos", "", "v1", None, 5, "r", identity_key("S1", "Hermanos"), "comfort")
    d = {"video_id": "v2", "title": "S2", "artist": "Ritmo"}         # DOM-style dict, no key
    ordering.attach_genres(store, [fy, d])
    assert fy.genre == "Latin"          # ForYouItem gets its genre from the library (by identity_key)
    assert d["genre"] == ""             # dict path works too; untagged -> empty (no crash, no journey)
