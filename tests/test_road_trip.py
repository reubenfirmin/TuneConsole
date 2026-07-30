def test_road_trip_recipe_crud(store):
    rid = store.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], ["synthpop"],
                                       ["country"], 240, 1000.0)
    assert isinstance(rid, int)

    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    r = recipes[0]
    assert r == {"id": rid, "name": "Beach Run", "own_pct": 60, "artists": ["Tame Impala"],
                "genres": ["synthpop"], "blacklist_genres": ["country"], "target_minutes": 240,
                "last_playlist_id": None, "created_at": 1000.0, "updated_at": 1000.0}

    got = store.get_road_trip_recipe(rid)
    assert got["name"] == "Beach Run"
    assert store.get_road_trip_recipe(rid + 1) is None

    store.save_road_trip_recipe(rid, "Beach Run 2", 70, ["Tame Impala", "MGMT"], ["synthpop"],
                                ["country"], 300, 2000.0)
    updated = store.get_road_trip_recipe(rid)
    assert updated["name"] == "Beach Run 2"
    assert updated["own_pct"] == 70
    assert updated["artists"] == ["Tame Impala", "MGMT"]
    assert updated["target_minutes"] == 300
    assert updated["updated_at"] == 2000.0

    store.set_road_trip_last_playlist(rid, "PL_NEW_0")
    assert store.get_road_trip_recipe(rid)["last_playlist_id"] == "PL_NEW_0"

    store.delete_road_trip_recipe(rid)
    assert store.list_road_trip_recipes() == []


from yt_playlist.rec import road_trip
from yt_playlist.rec.surfaces import ForYouItem


def _item(key, video_id, title, artist):
    return ForYouItem(title=title, artist=artist, album="Alb", video_id=video_id, thumbnail=None,
                      plays=0, reason="test", key=key, lane="test")


def test_build_own_pool_excludes_blacklisted_genres(store, monkeypatch):
    keep = _item("k_keep", "v_keep", "Keep Me", "Artist A")
    drop = _item("k_drop", "v_drop", "Drop Me", "Artist B")
    monkeypatch.setattr(road_trip.surfaces, "for_you", lambda store, now, limit: [keep, drop])
    monkeypatch.setattr(store, "keys_in_genre_selection", lambda tokens: {"k_drop"})

    pool = road_trip.build_own_pool(store, 1000.0, ["country"], limit=10)

    assert [t["video_id"] for t in pool] == ["v_keep"]
    assert pool[0]["source"] == "mine"
    assert pool[0]["title"] == "Keep Me"
    assert pool[0]["duration"] is None


def test_build_own_pool_no_blacklist_keeps_all(store, monkeypatch):
    a = _item("k_a", "v_a", "A", "Artist A")
    b = _item("k_b", "v_b", "B", "Artist B")
    monkeypatch.setattr(road_trip.surfaces, "for_you", lambda store, now, limit: [a, b])

    pool = road_trip.build_own_pool(store, 1000.0, [], limit=10)

    assert [t["video_id"] for t in pool] == ["v_a", "v_b"]


def test_build_own_pool_skips_items_without_video_id(store, monkeypatch):
    no_vid = _item("k_x", None, "No Video", "Artist X")
    monkeypatch.setattr(road_trip.surfaces, "for_you", lambda store, now, limit: [no_vid])

    pool = road_trip.build_own_pool(store, 1000.0, [], limit=10)

    assert pool == []


from tests.conftest import FakeClient


def test_artist_songs_resolves_via_search_then_get_artist(monkeypatch):
    # Real ytmusicapi shape (parsers/playlists.py:parse_playlist_item), NOT the simplified example
    # in get_artist's own docstring: "artists" is a list of {"name","id"} dicts, "album" is a
    # {"name","id"} dict or None - a plain "artist"/"album" string is never actually returned.
    client = FakeClient(
        search_results=[{"browseId": "UC1"}],
        artists={"UC1": {"songs": {"results": [
            {"videoId": "v1", "title": "Hit One", "artists": [{"name": "Tame Impala", "id": "UC1"}],
             "album": {"name": "Currents", "id": "MPRE1"}, "duration_seconds": 245},
            {"videoId": "v2", "title": "Hit Two", "artists": [{"name": "Tame Impala", "id": "UC1"}],
             "album": {"name": "Lonerism", "id": "MPRE2"}},
        ]}}})

    songs = road_trip._artist_songs(client, "Tame Impala")

    assert [s["video_id"] for s in songs] == ["v1", "v2"]
    assert songs[0]["source"] == "theirs"
    assert songs[0]["artist"] == "Tame Impala"
    assert songs[0]["album"] == "Currents"
    assert songs[0]["duration"] == 245
    assert songs[1]["duration"] is None   # no duration_seconds on this row -> falls back later


def test_artist_songs_unresolvable_artist_returns_empty():
    client = FakeClient(search_results=[])
    assert road_trip._artist_songs(client, "Nobody Band") == []


def test_sort_by_popularity_known_first_unknown_keep_order(monkeypatch):
    a = {"video_id": "va", "title": "A", "artist": "Art A"}
    b = {"video_id": "vb", "title": "B", "artist": "Art B"}
    c = {"video_id": "vc", "title": "C", "artist": "Art C"}
    pops = {("A", "Art A"): 100, ("C", "Art C"): 900}
    monkeypatch.setattr(road_trip, "_popularity",
                        lambda title, artist: pops.get((title, artist)))

    ordered = road_trip._sort_by_popularity([a, b, c])

    assert [t["video_id"] for t in ordered] == ["vc", "va", "vb"]


def test_genre_songs_normalizes_search_results():
    client = FakeClient(search_results=[
        {"videoId": "g1", "title": "Genre Hit", "artists": [{"name": "Genre Artist"}],
         "album": {"name": "Genre Alb"}, "duration_seconds": 210, "thumbnails": []},
    ])

    songs = road_trip._genre_songs(client, "synthpop")

    assert songs == [{"video_id": "g1", "title": "Genre Hit", "artist": "Genre Artist",
                      "album": "Genre Alb", "thumbnail": None, "duration": 210,
                      "source": "theirs"}]


def test_build_other_pool_merges_dedupes_and_caps_per_artist(monkeypatch):
    monkeypatch.setattr(road_trip, "_popularity", lambda title, artist: 500)   # stable order
    client = FakeClient()

    def fake_artist_songs(client_, name, limit=road_trip.ARTIST_SONGS_LIMIT):
        return [{"video_id": f"{name}_{i}", "title": f"{name} Song {i}", "artist": name,
                 "album": "", "thumbnail": None, "duration": None, "source": "theirs"}
                for i in range(5)]

    def fake_genre_songs(client_, genre, limit=road_trip.GENRE_SEARCH_LIMIT):
        return [{"video_id": "ArtistA_0", "title": "ArtistA Song 0", "artist": "ArtistA",
                 "album": "", "thumbnail": None, "duration": None, "source": "theirs"}]   # dup

    monkeypatch.setattr(road_trip, "_artist_songs", fake_artist_songs)
    monkeypatch.setattr(road_trip, "_genre_songs", fake_genre_songs)

    pool = road_trip.build_other_pool(client, ["ArtistA"], ["synthpop"], limit=20)

    video_ids = [t["video_id"] for t in pool]
    assert len(video_ids) == len(set(video_ids))               # no dupes
    assert video_ids.count("ArtistA_0") == 1                    # the genre-path dup collapsed
    assert sum(1 for t in pool if t["artist"] == "ArtistA") <= road_trip.DIVERSITY_ARTIST_CAP


from yt_playlist.rec import journeys


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


def _mk(vid, title, artist, source, duration=None):
    return {"video_id": vid, "title": title, "artist": artist, "album": "", "thumbnail": None,
            "duration": duration, "source": source}


def test_assemble_playlist_stops_at_target_duration(store, monkeypatch):
    # 10 own tracks and 10 other tracks, each 5 minutes (300s); own_pct=50, target=20 minutes
    own = [_mk(f"m{i}", f"Mine {i}", f"Mine Artist {i}", "mine", duration=300) for i in range(10)]
    other = [_mk(f"t{i}", f"Theirs {i}", f"Their Artist {i}", "theirs", duration=300) for i in range(10)]
    monkeypatch.setattr(road_trip, "build_own_pool", lambda store, now, bl, limit: own)
    monkeypatch.setattr(road_trip, "build_other_pool", lambda client, artists, genres, limit: other)

    recipe = {"own_pct": 50, "target_minutes": 20, "blacklist_genres": [], "artists": ["X"],
              "genres": ["Y"]}
    tracks, stats = road_trip.assemble_playlist(store, FakeClient(), recipe, now=1000.0, seed=0)

    total_s = sum(t["duration"] for t in tracks)
    assert total_s >= 20 * 60                       # hit or passed the target
    assert total_s < 20 * 60 + 300                   # never overshoots by more than one track
    assert stats["achieved_minutes"] == round(total_s / 60, 1)
    assert stats["own_count"] + stats["their_count"] == len(tracks)


def test_assemble_playlist_backfills_from_own_when_other_pool_is_thin(store, monkeypatch):
    own = [_mk(f"m{i}", f"Mine {i}", f"Mine Artist {i}", "mine", duration=300) for i in range(10)]
    other = [_mk("t0", "Theirs 0", "Their Artist 0", "theirs", duration=300)]   # only 1 available
    monkeypatch.setattr(road_trip, "build_own_pool", lambda store, now, bl, limit: own)
    monkeypatch.setattr(road_trip, "build_other_pool", lambda client, artists, genres, limit: other)

    recipe = {"own_pct": 20, "target_minutes": 30, "blacklist_genres": [], "artists": ["X"],
              "genres": []}
    tracks, stats = road_trip.assemble_playlist(store, FakeClient(), recipe, now=1000.0, seed=0)

    total_s = sum(t["duration"] for t in tracks)
    assert total_s >= 30 * 60                        # backfill still hit the target
    assert stats["their_count"] == 1                 # every "theirs" candidate got used, no more exist
    assert stats["own_count"] > 2                     # own pool topped up the shortfall


def test_assemble_playlist_resolves_missing_duration_via_store_then_client(store, monkeypatch):
    own = [_mk("m0", "Mine Zero", "Mine Artist", "mine", duration=None)]
    monkeypatch.setattr(road_trip, "build_own_pool", lambda store, now, bl, limit: own)
    monkeypatch.setattr(road_trip, "build_other_pool", lambda client, artists, genres, limit: [])
    client = FakeClient(song_durations={"m0": 187})

    recipe = {"own_pct": 100, "target_minutes": 1, "blacklist_genres": [], "artists": [], "genres": []}
    tracks, stats = road_trip.assemble_playlist(store, client, recipe, now=1000.0, seed=0)

    assert tracks[0]["duration"] == 187
    assert stats["achieved_minutes"] == round(187 / 60, 1)
