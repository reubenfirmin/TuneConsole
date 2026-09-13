from collections import Counter

import pytest

from yt_playlist.rec import journeys, road_trip
from yt_playlist.rec.surfaces import ForYouItem
from tests.conftest import FakeClient

# Captured before the autouse no_network fixture stubs it out, for the one test that exercises the
# real genre -> artists resolution rather than mocking past it.
_real_genre_artists = road_trip.genre_artists


def test_road_trip_recipe_crud(store):
    rid = store.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], ["synthpop"],
                                       240, 1000.0, familiarity_pct=80)
    assert isinstance(rid, int)

    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    r = recipes[0]
    assert r == {"id": rid, "name": "Beach Run", "own_pct": 60, "artists": ["Tame Impala"],
                "genres": ["synthpop"], "target_minutes": 240,
                "blacklist_genres": [],
                "familiarity_pct": 80, "last_playlist_id": None, "created_at": 1000.0,
                "updated_at": 1000.0}

    got = store.get_road_trip_recipe(rid)
    assert got["name"] == "Beach Run"
    assert store.get_road_trip_recipe(rid + 1) is None

    store.save_road_trip_recipe(rid, "Beach Run 2", 70, ["Tame Impala", "MGMT"], ["synthpop"],
                                300, 2000.0)
    updated = store.get_road_trip_recipe(rid)
    assert updated["name"] == "Beach Run 2"
    assert updated["own_pct"] == 70
    assert updated["artists"] == ["Tame Impala", "MGMT"]
    assert updated["target_minutes"] == 300
    assert updated["familiarity_pct"] == 50           # default when the caller doesn't pass one
    assert updated["updated_at"] == 2000.0

    store.set_road_trip_last_playlist(rid, "PL_NEW_0")
    assert store.get_road_trip_recipe(rid)["last_playlist_id"] == "PL_NEW_0"

    store.delete_road_trip_recipe(rid)
    assert store.list_road_trip_recipes() == []


def test_road_trip_draft_crud_and_recipe_delete_cascade(store):
    rid = store.save_road_trip_recipe(None, "Beach Run", 60, [], [], 60, 1000.0)
    assert store.get_road_trip_draft(rid) is None
    assert store.latest_road_trip_draft() is None

    store.save_road_trip_draft(rid, {"picks": ["v1", "v2"], "seed": 7}, 1000.0)
    assert store.get_road_trip_draft(rid) == {"picks": ["v1", "v2"], "seed": 7}
    assert store.latest_road_trip_draft() == {"recipe_id": rid,
                                              "state": {"picks": ["v1", "v2"], "seed": 7}}

    store.save_road_trip_draft(rid, {"picks": ["v3"], "seed": 8}, 2000.0)     # upsert, not insert
    assert store.get_road_trip_draft(rid)["picks"] == ["v3"]

    store.delete_road_trip_recipe(rid)                # deleting the recipe takes its draft with it
    assert store.get_road_trip_draft(rid) is None


# --------------------------------------------------------------------------- pools

def _item(key, video_id, title, artist, plays=0):
    return ForYouItem(title=title, artist=artist, album="Alb", video_id=video_id, thumbnail=None,
                      plays=plays, reason="test", key=key, lane="test")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every outbound lookup their side makes, stubbed: Deezer facts, the Last.fm artist genre, and
    the genre -> top-artists resolution. autouse, because a genre input now reaches three providers
    and a test that forgets one silently makes real network calls (it did).

    Popularity descends with the title length, so 'ArtistA Song 0' is the biggest hit."""
    monkeypatch.setattr(road_trip, "_facts",
                        lambda title, artist: {"popularity": 1000 - len(title), "year": 1995,
                                               "genre": "synthpop", "duration": 300})
    monkeypatch.setattr(road_trip, "artist_genre", lambda store, artist: None)
    monkeypatch.setattr(road_trip, "genre_artists",
                        lambda store, genre, decade=None, limit=12: [])
    return None


def _library(monkeypatch, store, songs):
    """Stand in for the whole collection. `songs` are (key, video_id, title, artist, plays, liked)."""
    monkeypatch.setattr(store, "library_songs", lambda: [
        {"key": k, "video_id": v, "title": ti, "artist": ar, "album": "Alb", "thumbnail": None,
         "duration": 300, "genre": "", "year": None, "liked": liked}
        for k, v, ti, ar, plays, liked in songs])
    monkeypatch.setattr(store, "play_counts", lambda: {
        k: plays for k, v, ti, ar, plays, liked in songs})


def test_own_side_is_the_whole_collection_ranked_by_engagement(store, monkeypatch):
    """Your side is not a sample of your library, it IS your library - spread along how much you
    actually listen, which is what the favorites/deeper-cuts slider slides along."""
    _library(monkeypatch, store, [
        ("k_a", "v_a", "Deep Cut", "Artist A", 0, False),
        ("k_b", "v_b", "Favorite", "Artist B", 40, False),
        ("k_c", "v_c", "Liked But Unplayed", "Artist C", 0, True)])

    pool = road_trip.own_candidates(store, 1000.0)

    assert {c["video_id"] for c in pool} == {"v_a", "v_b", "v_c"}
    assert all(c["source"] == "mine" for c in pool)
    by_id = {c["video_id"]: c for c in pool}
    assert by_id["v_b"]["fam"] > by_id["v_c"]["fam"] > by_id["v_a"]["fam"]
    assert by_id["v_c"]["engagement"] == road_trip.LIKE_BONUS   # a like counts as listening


def test_own_side_honours_the_taste_models_exclusions(store, monkeypatch):
    _library(monkeypatch, store, [("k_a", "v_a", "Keep", "Artist A", 1, False),
                                  ("k_b", "v_b", "Dismissed", "Artist B", 1, False),
                                  ("k_c", "v_c", "Muted", "Muted Artist", 1, False)])
    monkeypatch.setattr(store, "suppressed_keys", lambda surface, now: {"k_b"})
    monkeypatch.setattr(store, "muted_artists", lambda: {"Muted Artist"})

    assert [c["video_id"] for c in road_trip.own_candidates(store, 1000.0)] == ["v_a"]


def test_own_side_honours_recipe_genre_blacklist(store, monkeypatch):
    _library(monkeypatch, store, [("k_a", "v_a", "Keep", "Artist A", 1, False),
                                  ("k_b", "v_b", "Blocked", "Artist B", 1, False)])
    monkeypatch.setattr(store, "keys_in_genre_selection", lambda genres: {"k_b"})

    state = {"blacklist_genres": ["Metal"]}
    assert [c["video_id"] for c in road_trip.own_candidates(store, 1000.0, state)] == ["v_a"]


def test_artist_songs_resolves_via_search_then_get_artist():
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
    assert songs[0]["artist"] == "Tame Impala"
    assert songs[0]["album"] == "Currents"
    assert songs[0]["duration"] == 245
    assert songs[1]["duration"] is None   # no duration_seconds on this row -> filled in later


def test_artist_songs_unresolvable_artist_returns_empty():
    assert road_trip._artist_songs(FakeClient(search_results=[]), "Nobody Band") == []


def test_genre_songs_keeps_the_search_term_as_the_genre():
    client = FakeClient(search_results=[
        {"videoId": "g1", "title": "Genre Hit", "artists": [{"name": "Genre Artist"}],
         "album": {"name": "Genre Alb"}, "duration_seconds": 210, "thumbnails": []},
    ])

    songs = road_trip._genre_songs(client, "synthpop")

    assert songs == [{"video_id": "g1", "title": "Genre Hit", "artist": "Genre Artist",
                      "album": "Genre Alb", "thumbnail": None, "duration": 210,
                      "genre": "synthpop"}]


def test_a_genre_resolves_to_its_top_artists_then_their_youtube_tracks(monkeypatch):
    """A genre input goes through the music databases, not a YouTube text search: ask who plays the
    genre, then ask YouTube for those artists' biggest tracks. Searching YouTube for a genre STRING
    returns mixes, karaoke and "top 100" uploads."""
    monkeypatch.setattr(road_trip, "genre_artists",
                        lambda store, genre, decade=None, limit=12: ["Weezer", "Pixies"])
    monkeypatch.setattr(road_trip, "_artist_songs",
                        lambda client, name, limit=30: _fake_artist_songs(4)(None, name))
    searched = []
    monkeypatch.setattr(road_trip, "_genre_songs",
                        lambda client, genre, limit=30: searched.append(genre) or [])

    rows, related = road_trip.other_input_songs(None, "genre", "Alternative Rock", cap=8)

    assert {r["artist"] for r in rows} == {"Weezer", "Pixies"}
    assert all(r["genre"] == "Alternative Rock" for r in rows)   # tagged with what brought them in
    assert searched == [], "the raw genre search is a last resort, not the first move"


def test_a_genre_no_database_knows_falls_back_to_searching_youtube(monkeypatch):
    monkeypatch.setattr(road_trip, "genre_artists", lambda store, genre, decade=None, limit=12: [])
    monkeypatch.setattr(road_trip, "_genre_songs", lambda client, genre, limit=30: [
        {"video_id": "g1", "title": "Hit", "artist": "Someone", "album": "", "thumbnail": None,
         "duration": 200, "genre": genre}])

    rows, _ = road_trip.other_input_songs(None, "genre", "bagpipe funk", cap=8)

    assert [r["video_id"] for r in rows] == ["g1"]


def test_passenger_genre_grows_the_seed_artists_neighborhood_and_era(store, monkeypatch):
    catalog = {
        "Weezer": (1994, "Alternative Rock", ["Peer A", "Modern Revival", "Jazz Peer"]),
        "Staind": (1999, "Alternative Rock", ["Peer B", "Peer A"]),
        "Peer A": (1996, "Alternative Rock", []),
        "Peer B": (2002, "Alternative Rock", []),
        "Modern Revival": (2024, "Alternative Rock", []),
        "Jazz Peer": (1996, "Jazz", []),
        "Unrelated": (1996, "Alternative Rock", []),
    }

    def page(client, name):
        return {"songs": {"results": [{"videoId": name, "title": name,
                 "artists": [{"name": name}], "duration_seconds": 240}]},
                "related": {"results": [{"title": n} for n in catalog[name][2]]}}, name

    monkeypatch.setattr(road_trip, "_artist_page", page)
    monkeypatch.setattr(road_trip, "_facts", lambda title, artist: {
        "year": catalog[artist][0], "genre": catalog[artist][1], "popularity": 500, "duration": 240})
    monkeypatch.setattr(road_trip, "artist_genre", lambda store, artist: catalog[artist][1])
    monkeypatch.setattr(road_trip, "genre_artists", lambda *a, **kw: pytest.fail("Broad genre lookup"))
    monkeypatch.setattr(road_trip, "_genre_songs", lambda *a, **kw: pytest.fail("Broad genre search"))

    state = road_trip.build_draft(store, FakeClient(), _recipe(
        own_pct=0, artists=["Weezer", "Staind"], genres=["Alternative Rock"]), 1000, seed=7)

    assert {c["artist"] for c in state["pool"]} == {"Weezer", "Staind", "Peer A", "Peer B"}
    axis = next(a for a in state["axes"]["theirs"] if a["key"] == "genre:Alternative Rock")
    assert axis["artists"] == ["Weezer", "Staind"]
    assert all(c["cluster_genre"] == "Alternative Rock" for c in state["pool"])

    # Older drafts can contain a broad genre pool. The next slider move replaces those results
    # with the same seed neighborhood, even if there are already enough generic tracks.
    for name in ("Unrelated", "Modern Revival"):
        candidate = road_trip._candidate(name, name, name, "", None, 240, "theirs",
                                         "Alternative Rock", catalog[name][0])
        candidate["input"] = "Alternative Rock"
        state["pool"].append(candidate)
    state.pop("artist_contexts")  # an older persisted draft has to recover its anchors too
    road_trip.set_share(state, "theirs", "genre:Alternative Rock", .8, store, 1000)
    assert state["pending"]
    while state["pending"]:
        road_trip.add_other_input(state, store, FakeClient(), state["pending"].pop(0))
    road_trip.finish_draft(state, store, 1000)
    assert {c["artist"] for c in state["pool"]} == {"Weezer", "Staind", "Peer A", "Peer B"}
    assert state["targets"]["theirs"]["genre:Alternative Rock"] == .8

    edited = _recipe(own_pct=0, artists=["Staind"], genres=["Alternative Rock"])
    road_trip.apply_recipe(state, store, 1000, edited)
    while state["pending"]:
        road_trip.add_other_input(state, store, FakeClient(), state["pending"].pop(0))
    road_trip.finish_draft(state, store, 1000)
    assert "Weezer" not in {c["artist"] for c in state["pool"]}
    assert set(state["artist_contexts"]) == {"Staind"}


@pytest.mark.parametrize("with_store", [False, True])
def test_passenger_sliders_keep_each_seeds_genre_and_repair_old_assignments(store, monkeypatch, with_store):
    seeds = ["Weezer", "We Were Promised Jetpacks", "Staind"]
    catalog = {
        "Weezer": ("Alternative Rock", ["Alt Peer", "We Were Promised Jetpacks"]),
        "We Were Promised Jetpacks": ("Indie Rock", ["Indie Peer", "Weezer"]),
        "Staind": ("Alternative Rock", ["Alt Peer"]),
        "Alt Peer": ("Alternative Rock", []),
        "Indie Peer": ("Indie Rock", []),
    }

    def page(client, name):
        return {"songs": {"results": [{"videoId": name, "title": name,
                 "artists": [{"name": name}], "duration_seconds": 240}]},
                "related": {"results": [{"title": n} for n in catalog[name][1]]}}, name

    monkeypatch.setattr(road_trip, "_artist_page", page)
    monkeypatch.setattr(road_trip, "_facts", lambda title, artist: {"popularity": 500})
    monkeypatch.setattr(road_trip, "artist_genre", lambda store, artist: catalog[artist][0])
    monkeypatch.setattr(road_trip, "genre_artists", lambda *a, **kw: pytest.fail("Broad genre lookup"))
    monkeypatch.setattr(road_trip, "_genre_songs", lambda *a, **kw: pytest.fail("Broad genre search"))
    state = road_trip.build_draft(store, FakeClient(), _recipe(
        own_pct=0, artists=seeds, genres=["alt rock", "indie rock"]), 1000, seed=7)
    expected = {"Alternative Rock": ["Weezer", "Staind"], "Indie Rock": [seeds[1]]}
    assert {a["name"]: a["artists"] for a in state["axes"]["theirs"]
            if a["kind"] == "genre"} == expected

    # Reproduce cached cross-family cluster assignments and a removed artist's lingering genre.
    for candidate in state["pool"]:
        candidate["cluster_genre"] = "Alternative Rock"
    extra = road_trip._candidate("old-peer", "Old peer track", "Alt Peer", "", None, 240,
                                 "theirs", "Alternative Rock", None)
    extra.update(cluster_genre="Post-Punk", input="Post-Punk", anchors=seeds)
    state["pool"].append(extra)
    for name, origin in (("Ben Folds", "Ben Folds Five"), ("Generic Singer", "Singer-Songwriter")):
        candidate = road_trip._candidate(name, name, name, "", None, 240,
                                         "theirs", "Singer-Songwriter", None)
        candidate["input"] = origin
        state["pool"].append(candidate)
    for genre in ("Post-Punk", "Singer-Songwriter"):
        state["axes"]["theirs"].append({"key": "genre:" + genre, "kind": "genre",
                                        "name": genre, "share": .25})
    for axis in state["axes"]["theirs"]:
        axis.update(artists=seeds, target=.25)
        state["targets"]["theirs"][axis["key"]] = .25
    state["picked"] = [{k: c[k] for k in road_trip._ROW_FIELDS} for c in state["pool"]]
    state["picks"] = [c["video_id"] for c in state["picked"]]

    road_trip.normalized(state, store if with_store else None, 1000)

    assert {a["name"]: a["artists"] for a in state["axes"]["theirs"]} == expected
    assert sum(a["balance_pct"] for a in state["axes"]["theirs"]) == 100
    assert set(state["targets"]["theirs"]) == {"genre:" + g for g in expected}
    assert {c["artist"] for c in state["pool"]} == set(catalog)
    assert all(c["cluster_genre"] == catalog[c["artist"]][0] for c in state["pool"])
    assert all(c["cluster_genre"] == catalog[c["artist"]][0] for c in state["picked"])

    # Neither genre can pull the other selected seeds back in, even via a related-artist list.
    for genre, artists in (("Alternative Rock", {"Weezer", "Staind", "Alt Peer"}),
                            ("Indie Rock", {seeds[1], "Indie Peer"})):
        road_trip.set_share(state, "theirs", "genre:" + genre, 1, store, 1000)
        while state["pending"]:
            road_trip.add_other_input(state, store, FakeClient(), state["pending"].pop(0))
        road_trip.finish_draft(state, store, 1000)
        assert state["picked"]
        assert {c["artist"] for c in state["picked"]} <= artists


def test_an_explicit_passenger_genre_keeps_its_origin_when_widened(store, monkeypatch):
    state = road_trip.start_draft(store, _recipe(
        own_pct=0, artists=["Weezer"], genres=["post-rock"]), 1000, seed=7)
    state["artist_contexts"] = {"Weezer": {"genre": "Alternative Rock", "year": None, "related": []}}
    state["pending"] = []
    calls = []

    def songs(client, kind, name, cap, store=None, decade=None):
        calls.append(name)
        return ([{"video_id": f"post-{len(calls)}", "title": "Post song", "artist": "Post Band",
                  "album": "", "thumbnail": None, "duration": 300, "genre": "Post-Punk"}], [])

    monkeypatch.setattr(road_trip, "other_input_songs", songs)
    road_trip.add_other_input(state, store, FakeClient(), {"kind": "genre", "name": "post-rock"})
    road_trip.finish_draft(state, store, 1000)
    assert state["axes"]["theirs"][0]["artists"] == []

    road_trip.set_share(state, "theirs", "genre:Post-Punk", 1, store, 1000)
    while state["pending"]:
        road_trip.add_other_input(state, store, FakeClient(), state["pending"].pop(0))
    road_trip.finish_draft(state, store, 1000)
    assert calls == ["post-rock", "Post-Punk"]
    assert len(state["pool"]) == 2
    assert all(c["genre_input"] == "post-rock" for c in state["pool"])
    assert next(a for a in state["axes"]["theirs"] if a["kind"] == "genre")["artists"] == []

    # Removing the original input also removes the tracks later fetched through its slider.
    road_trip.apply_recipe(state, store, 1000, _recipe(own_pct=0, artists=["Weezer"], genres=[]))
    assert state["pool"] == []
    assert not [a for a in state["axes"]["theirs"] if a["kind"] == "genre"]


def test_genre_artists_prefers_lastfm_and_uses_musicbrainz_for_a_decade(monkeypatch):
    monkeypatch.setattr(road_trip.lastfm, "api_key", lambda store=None: "k")
    monkeypatch.setattr(road_trip.lastfm, "tag_top_artists",
                        lambda tag, key, limit=25: ["Weezer", "Pixies"])
    mb_calls = []

    def mb(tag, decade=None, limit=25):
        mb_calls.append((tag, decade))
        return ["Pixies", "Interpol", "Various Artists"] if decade else ["Filler"]

    monkeypatch.setattr(road_trip.musicbrainz, "tag_artists", mb)
    road_trip._GENRE_ARTIST_CACHE.clear()

    assert _real_genre_artists(None, "Alternative Rock") == ["Weezer", "Pixies"]
    assert mb_calls == []                          # Last.fm answered; no need to ask further

    road_trip._GENRE_ARTIST_CACHE.clear()
    got = _real_genre_artists(None, "Alternative Rock", decade="2000")

    assert mb_calls == [("Alternative Rock", "2000")]   # only MB can filter on a decade
    assert got[0] == "Pixies"          # both sources agree on Pixies, so it leads
    assert got[1:] == ["Weezer", "Interpol"]   # then their leftovers, interleaved
    assert "Various Artists" not in got         # not an artist; chasing it returns compilations


def _fake_artist_songs(count, prefix="ArtistA"):
    def fake(client_, name, limit=road_trip.ARTIST_SONGS_LIMIT):
        return [{"video_id": f"{name}_{i}", "title": f"{name} Song {i}", "artist": name,
                 "album": "", "thumbnail": None, "duration": 300, "genre": ""}
                for i in range(count)]
    return fake


def test_build_other_pool_cap_scales_with_demand(monkeypatch):
    """A single named artist must be able to fill their whole half. A fixed per-artist cap is what
    made a 50% "theirs" recipe come back almost entirely "mine"."""
    monkeypatch.setattr(road_trip, "_page_songs",
                        lambda page, name, limit=30: _fake_artist_songs(25)(None, name))
    monkeypatch.setattr(road_trip, "_artist_page", lambda client, name: ({}, "UC1"))

    pool = road_trip.build_other_pool(FakeClient(), ["ArtistA"], [], limit=20)

    assert len(pool) == 20
    assert all(t["source"] == "theirs" for t in pool)


def test_build_other_pool_dedupes_and_spreads_across_inputs(monkeypatch):
    monkeypatch.setattr(road_trip, "_page_songs",
                        lambda page, name, limit=30: _fake_artist_songs(6)(None, name))
    monkeypatch.setattr(road_trip, "_artist_page", lambda client, name: ({}, "UC1"))
    monkeypatch.setattr(road_trip, "_genre_songs", lambda client, genre, limit=30: [
        {"video_id": "ArtistA_0", "title": "ArtistA Song 0", "artist": "ArtistA",     # a dup
         "album": "", "thumbnail": None, "duration": 300, "genre": genre},
        {"video_id": "syn_1", "title": "Synth Song", "artist": "Synth Band", "album": "",
         "thumbnail": None, "duration": 300, "genre": genre}])

    pool = road_trip.build_other_pool(FakeClient(), ["ArtistA", "ArtistB"], ["synthpop"], limit=12)

    ids = [t["video_id"] for t in pool]
    assert len(ids) == len(set(ids))
    assert ids.count("ArtistA_0") == 1
    assert {"ArtistA", "ArtistB", "Synth Band"} <= {t["artist"] for t in pool}


def test_build_other_pool_widens_to_related_artists_when_thin(monkeypatch):
    page = {"related": {"results": [{"title": "Cousin Band"}]}}
    monkeypatch.setattr(road_trip, "_artist_page", lambda client, name: (page, "UC1"))
    monkeypatch.setattr(road_trip, "_page_songs",
                        lambda p, name, limit=30: _fake_artist_songs(2)(None, name))
    monkeypatch.setattr(road_trip, "_artist_songs",
                        lambda client, name, limit=30: _fake_artist_songs(6)(None, name))

    pool = road_trip.build_other_pool(FakeClient(), ["ArtistA"], [], limit=8)

    assert "Cousin Band" in {t["artist"] for t in pool}


# --------------------------------------------------------------------------- draft picking

def _recipe(**kw):
    base = {"id": 1, "name": "Beach Run", "own_pct": 50, "familiarity_pct": 50,
            "target_minutes": 60, "artists": ["ArtistA"], "genres": []}
    return {**base, **kw}


def _stub_pools(monkeypatch, store=None, own_count=40, other_count=40, own_plays=None,
                own_genre=lambda i: "Rock", own_year=lambda i: 1990 + (i % 3) * 10):
    """A library of 5-minute songs on your side and an equally deep pool on theirs, so ratio
    assertions are exact. Your side is stubbed at the library query - the real seam now - and theirs
    at the per-input fetch."""
    songs = [{"key": f"k{i}", "video_id": f"m{i}", "title": f"Mine {i}",
              "artist": f"Mine Artist {i}", "album": "", "thumbnail": None, "duration": 300,
              "genre": own_genre(i), "year": own_year(i), "liked": False}
             for i in range(own_count)]
    if store is not None:
        monkeypatch.setattr(store, "library_songs", lambda: songs)
        monkeypatch.setattr(store, "play_counts",
                            lambda: {f"k{i}": (own_plays or (lambda i: i))(i) for i in range(own_count)})

    def songs_fn(client, kind, name, cap, store=None, decade=None):
        rows = [{"video_id": f"t{i}", "title": f"Theirs {i}", "artist": f"Their Artist {i}",
                 "album": "", "thumbnail": None, "duration": 300, "genre": "synthpop"}
                for i in range(other_count)]
        return rows[:cap], []

    monkeypatch.setattr(road_trip, "other_input_songs", songs_fn)
    monkeypatch.setattr(road_trip, "_facts",
                        lambda title, artist: {"popularity": 900 - int(title.split()[-1]),
                                               "year": 1980, "genre": "synthpop", "duration": 300})


def test_build_draft_honours_the_requested_mix(store, monkeypatch):
    """The headline bug: a 50/50 recipe has to come back roughly 50/50."""
    _stub_pools(monkeypatch, store)

    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=50), 1000.0, seed=1)

    stats = state["stats"]
    assert stats["own_count"] == stats["their_count"] == 6      # 60 min of 5-minute tracks
    assert stats["short"] == {}
    assert 55 <= stats["minutes"] <= 65


def test_default_mix_identifies_yours_theirs_and_blend_through_swaps_and_reload(store, monkeypatch):
    _stub_pools(monkeypatch, store, own_genre=lambda i: "Rock" if i % 2 == 0 else "Jazz")

    state = road_trip.build_draft(
        store, FakeClient(), _recipe(own_pct=50, genres=["Rock"]), 1000.0, seed=1)

    assert state["stats"]["overlap_count"] == 4
    assert state["stats"]["own_count"] == 8       # library accounting includes the shared pool
    assert state["stats"]["their_count"] == 4
    assert state["stats"]["yours_count"] == state["stats"]["blend_count"] == 4
    assert Counter(c["mix_source"] for c in state["picked"]) == {"yours": 4, "theirs": 4, "blend": 4}
    assert {c["genre"] for c in state["picked"] if c["mix_source"] == "blend"} == {"Rock"}
    for source in ("yours", "theirs", "blend"):
        index = next(i for i, c in enumerate(state["picked"]) if c["mix_source"] == source)
        previous = state["picked"][index]["video_id"]
        road_trip.reroll_slot(state, store, index, 1000)
        assert state["picked"][index]["video_id"] != previous
        assert state["picked"][index]["mix_source"] == source
    # Stored drafts from before source labels existed must show the same three pools on reload.
    for candidate in state["picked"]:
        candidate.pop("mix_source")
    for key in ("yours_count", "blend_count", "yours_minutes", "blend_minutes"):
        state["stats"].pop(key)
    state.pop("blend_available")
    road_trip.normalized(state)
    assert Counter(c["mix_source"] for c in state["picked"]) == {"yours": 4, "theirs": 4, "blend": 4}
    assert state["stats"]["yours_count"] == state["stats"]["blend_count"] == 4
    assert state["stats"]["yours_minutes"] == state["stats"]["blend_minutes"] == 20
    assert state["blend_available"] is True


def test_blend_sources_and_totals_include_shortfall_replacements(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=2,
                own_genre=lambda i: "Rock" if i % 2 == 0 else "Jazz")
    state = road_trip.build_draft(
        store, FakeClient(), _recipe(own_pct=50, genres=["Rock"]), 1000, seed=1)
    assert state["stats"]["short"]["theirs"] > 0
    for candidate in state["picked"]:
        if candidate["source"] == "mine":
            assert candidate["mix_source"] == ("blend" if candidate["genre"] == "Rock" else "yours")
    counts = Counter(c["mix_source"] for c in state["picked"])
    assert counts == {"yours": 6, "theirs": 2, "blend": 4}
    assert state["stats"]["yours_count"] == counts["yours"]
    assert state["stats"]["blend_count"] == counts["blend"]
    assert sum(counts.values()) == state["stats"]["own_count"] + state["stats"]["their_count"]


@pytest.mark.parametrize("own_pct, uneven_lengths", [(50, False), (50, True), (0, True), (100, True)])
def test_blend_reserves_song_slots_with_library_genre_and_era_targets(
        store, monkeypatch, own_pct, uneven_lengths):
    genres = ["Rock", "Pop", "Trance", "Deep House"]
    _stub_pools(monkeypatch, store, own_count=160, other_count=80,
                own_genre=lambda i: genres[i % 4], own_year=lambda i: 1990 if i % 4 == 0 else 2000)
    state = road_trip.build_draft(store, FakeClient(),
                                  _recipe(own_pct=own_pct, genres=["Rock"], target_minutes=240),
                                  1000, seed=7)
    # Bypass the initial per-input discovery cap so every requested mix has ample candidates.
    state["pool"] += [{**state["pool"][0], "video_id": f"extra-{i}", "artist": f"Extra artist {i}"}
                      for i in range(40)]
    state["targets"]["mine"] = {"genre:Rock": .34, "genre:Pop": .19,
                                "genre:Trance": .14, "genre:Deep House": .33,
                                "era:1990": .34, "era:2000": .66}
    if uneven_lengths:
        # Blend songs take ten minutes; Theirs takes three. The source percentages still refer
        # to song counts, and Yours' absent genres/years must not consume Blend's reserved slots.
        durations = {"Rock": 600, "Pop": 240, "Trance": 480, "Deep House": 360}
        for candidate in store.library_songs():
            candidate["duration"] = durations[candidate["genre"]]
        for candidate in state["pool"]:
            candidate["duration"] = 180
    road_trip.repick(state, store, 1000)
    counts = Counter(c["mix_source"] for c in state["picked"])
    total = sum(counts.values())
    assert abs(counts["blend"] - total / 3) <= 1
    assert abs(counts["yours"] - total * 2 / 3 * own_pct / 100) <= 1
    assert abs(state["stats"]["minutes"] - 240) <= 6
    assert not state["stats"]["short"]
    assert counts == {k: v for k, v in state["stats"]["mix_targets"].items() if v}
    assert {c["genre"] for c in state["picked"] if c["mix_source"] == "blend"} == {"Rock"}
    assert {c["year"] for c in state["picked"] if c["mix_source"] == "blend"} == {1990}


@pytest.mark.parametrize("rock_share", [0, .34])
def test_blend_shortage_is_reported_without_overriding_exclusions(store, monkeypatch, rock_share):
    _stub_pools(monkeypatch, store, own_genre=lambda i: "Rock" if i < 2 else "Jazz")
    state = road_trip.build_draft(store, FakeClient(), _recipe(genres=["Rock"]), 1000, seed=7)
    state["targets"]["mine"] = {"genre:Rock": rock_share, "genre:Jazz": 1 - rock_share}
    road_trip.repick(state, store, 1000)
    counts = Counter(c["mix_source"] for c in state["picked"])
    assert sum(counts.values()) == 12
    assert counts["blend"] == (2 if rock_share else 0)
    assert state["stats"]["short"]["blend"] > 0
    assert state["stats"]["mix_targets"]["blend"] == 4
    assert state["stats"]["minutes"] == 60
    if not rock_share:
        assert all(c["genre"] != "Rock" for c in state["picked"])


def test_build_draft_honours_a_lopsided_mix(store, monkeypatch):
    _stub_pools(monkeypatch, store)

    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=20), 1000.0, seed=1)

    assert state["stats"]["own_count"] == 2            # 12 min of your side, to the nearest track
    assert state["stats"]["their_count"] == 10          # 48 min of theirs


def test_build_draft_reports_when_their_side_runs_short(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=2)

    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=50), 1000.0, seed=1)

    assert state["stats"]["their_count"] == 2          # everything they had
    assert state["stats"]["short"]["theirs"] > 0        # and it says so, instead of pretending
    assert state["stats"]["own_count"] > 6              # your side covered the rest


def test_repeated_builds_produce_different_playlists(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    recipe = _recipe()

    runs, previous = [], []
    for seed in (11, 22, 33, 44):
        state = road_trip.build_draft(store, FakeClient(), recipe, 1000.0, seed, previous)
        previous = state["picks"]
        runs.append(tuple(state["picks"]))

    assert len(set(runs)) == 4                                  # four runs, four playlists
    for a, b in zip(runs, runs[1:]):                            # and consecutive ones barely overlap
        assert len(set(a) & set(b)) <= len(a) // 2


def test_same_seed_rebuilds_the_same_playlist(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    a = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=7)
    b = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=7)
    assert a["picks"] == b["picks"]


def test_familiarity_slider_moves_between_favorites_and_deep_cuts(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    plays = {}

    def played(state):   # the stub gives m<i> exactly i plays
        rows = state["picked"]
        return sum(int(r["video_id"][1:]) for r in rows) / max(1, len(rows))

    for pct in (0, 100):
        state = road_trip.build_draft(store, FakeClient(),
                                      _recipe(own_pct=100, familiarity_pct=pct), 1000.0, seed=3)
        plays[pct] = played(state)

    assert plays[100] > plays[0] * 2    # favorites at one end, lesser listens at the other
    assert plays[100] > 20 > plays[0]   # and each end sits in its half of the collection


def _share(state, party, axis):
    return next(a["share"] for a in state["axes"][party] if a["key"] == axis)


def test_a_slider_sits_at_the_share_that_genre_actually_has(store, monkeypatch):
    """The complaint this replaced: a 0-2 steering weight that always started centred told you
    nothing about the playlist. A bar now reads as the proportion in the list below it."""
    _stub_pools(monkeypatch, store, own_count=0)          # their side only: one genre, so it is all of it
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=0), 1000.0, seed=5)

    axis = next(a["key"] for a in state["axes"]["theirs"] if a["kind"] == "genre")
    assert _share(state, "theirs", axis) == 1.0
    assert all(a["target"] is None for a in state["axes"]["theirs"])     # nothing pinned yet


def test_sliding_one_decade_to_100_empties_the_others(store, monkeypatch):
    """Dragging a bar to 100% has to mean it: the other decades go, rather than quietly refilling
    the slots the moment that decade runs out. A quota, not a weight."""
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    decades = {r["decade"] for r in state["picked"]}
    assert len(decades) > 1                                  # a mix of decades to start with

    road_trip.set_share(state, "mine", "era:2010", 1.0, store)

    assert {r["decade"] for r in state["picked"]} == {"2010"}
    assert _share(state, "mine", "era:2010") == 1.0
    assert all(a["share"] == 0.0 for a in state["axes"]["mine"]
               if a["kind"] == "era" and a["key"] != "era:2010")


def test_asking_for_more_of_a_genre_than_the_pool_has_goes_and_finds_it(store, monkeypatch):
    """Their side is pulled fresh from YouTube, so a slider is not limited to what was drawn first:
    asking for 100% of a genre queues searches to deepen the pool, and what comes back is filtered
    to that genre so a loose search can't pollute the mix."""
    _stub_pools(monkeypatch, store, own_count=10, other_count=4)
    state = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=5)
    axis = next(a["key"] for a in state["axes"]["theirs"] if a["kind"] == "genre")
    assert not state["pending"]

    road_trip.set_share(state, "theirs", axis, 1.0, store)

    assert state["building"] is True
    assert [p["want"] for p in state["pending"]] == [axis] * len(state["pending"])
    assert state["pending"], "a pin the pool can't cover should queue a search, not give up"

    # The queued searches return a mix; only the pinned genre survives into the pool.
    def songs(client, kind, name, cap, store=None, decade=None):
        return ([{"video_id": f"w{i}", "title": f"Wide {i}", "artist": f"Wide Artist {i}",
                  "album": "", "thumbnail": None, "duration": 300,
                  "genre": "synthpop" if i % 2 == 0 else "polka"} for i in range(6)], [])
    monkeypatch.setattr(road_trip, "other_input_songs", songs)
    monkeypatch.setattr(road_trip, "_facts", lambda t, a: {"popularity": 500, "year": 1985,
                                                           "genre": "synthpop" if "0" in t or "2" in t
                                                           or "4" in t else "polka", "duration": 300})
    while state["pending"]:
        road_trip.add_other_input(state, store, FakeClient(), state["pending"].pop(0))

    widened = [c for c in state["pool"] if c["video_id"].startswith("w")]
    assert widened, "the search results should have reached the pool"
    assert all(road_trip._matches_axis(c, axis) for c in widened)


def test_a_pin_survives_a_slot_being_crossed_out(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    road_trip.set_share(state, "mine", "era:2010", 1.0, store)
    by_id = {c["video_id"]: c for c in state["pool"]}

    road_trip.reroll_slot(state, store, 0)

    assert {r["decade"] for r in state["picked"]} == {"2010"}


def test_moving_another_slider_rebalances_the_previous_targets(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)

    road_trip.set_share(state, "mine", "era:2010", 0.5, store)
    before = dict(state["targets"]["mine"])
    road_trip.set_share(state, "mine", "era:1990", 0.5, store)

    decades = Counter(r["decade"] for r in state["picked"])
    targets = state["targets"]["mine"]
    assert targets["era:1990"] == 0.5
    assert sum(v for k, v in targets.items() if k.startswith("era:")) == pytest.approx(1)
    assert targets["era:2010"] / targets["era:2000"] == pytest.approx(
        before["era:2010"] / before["era:2000"], abs=.1)
    assert decades["1990"] == sum(decades.values()) // 2


@pytest.mark.parametrize("initial, edited, requested, expected", [
    ([50, 30, 20], 0, 20, [20, 48, 32]),
    ([20, 48, 32], 1, 60, [15, 60, 25]),
    ([50, 30, 20], 0, 100, [100, 0, 0]),
    ([100, 0, 0], 0, 40, [40, 30, 30]),
    ([0, 0, 0], 0, 33, [33, 34, 33]),
])
def test_balance_percentages_sum_to_100(initial, edited, requested, expected):
    axes = [{"key": f"genre:{i}", "kind": "genre", "share": p / 100}
            for i, p in enumerate(initial)]
    state = {"axes": {"theirs": axes},
             "targets": {"theirs": {"era:1990": .7}, "mine": {"genre:Rock": .4}}}
    road_trip._rebalance_targets(state, "theirs", f"genre:{edited}", requested / 100)
    assert [state["targets"]["theirs"][f"genre:{i}"] for i in range(3)] == [p / 100 for p in expected]
    assert sum(expected) == 100
    assert state["targets"]["theirs"]["era:1990"] == .7
    assert state["targets"]["mine"] == {"genre:Rock": .4}


@pytest.mark.parametrize("initial, additions, expected", [
    ([60, 30, 10], ["Funk"], [45, 23, 7, 25]),
    ([80, 20], ["Funk"], [54, 13, 33]),
    ([100, 0], ["Funk"], [67, 0, 33]),
    ([60, 40], ["Funk", "Soul"], [30, 20, 25, 25]),
    ([], ["Funk"], [100]),
    ([], ["Funk", "Soul"], [50, 50]),
])
def test_new_passenger_genres_get_an_equal_share_of_the_new_count(initial, additions, expected):
    state = {"axes": {"theirs": [
        {"key": "genre:" + g, "kind": "genre", "name": g, "share": p / 100}
        for g, p in zip(["Rock", "Pop", "Jazz"], initial)]},
        "targets": {"mine": {"genre:Blues": .6}, "theirs": {"era:1990": .8}}}
    road_trip._add_genre_targets(state, additions)
    road_trip.normalized(state)
    rows = [a for a in state["axes"]["theirs"] if a["kind"] == "genre"]
    assert [a["balance_pct"] for a in rows] == expected
    assert sum(a["balance_pct"] for a in rows) == 100
    assert state["targets"]["mine"] == {"genre:Blues": .6}
    assert state["targets"]["theirs"]["era:1990"] == .8
    # Repeated saves and aliases must preserve the existing balance and avoid duplicate rows.
    road_trip._add_genre_targets(state, [g.lower() for g in additions])
    road_trip.normalized(state)
    assert [a["balance_pct"] for a in state["axes"]["theirs"] if a["kind"] == "genre"] == expected


@pytest.mark.parametrize("seed_tracks", [20, 1])
@pytest.mark.parametrize("alt_share", [.5, 0])
def test_named_artists_lead_each_genre_with_related_tracks_filling_the_rest(store, seed_tracks, alt_share):
    artists = {"Alternative Rock": ["Weezer", "Staind"], "Indie Rock": ["We Were Promised Jetpacks"]}
    state = road_trip.start_draft(store, _recipe(
        own_pct=0, target_minutes=120, artists=sum(artists.values(), []), genres=[]), 1000, seed=7)
    for genre, seeds in artists.items():
        # Many more, higher-scoring discovery tracks than named-artist tracks: popularity and
        # the general per-artist cap must not drown out the artists the passenger selected.
        catalog = [(name, seed_tracks, .05) for name in seeds]
        catalog += [(f"{genre} Peer {i}", 4, 1) for i in range(12)]
        for name, count, score in catalog:
            for i in range(count):
                candidate = road_trip._candidate(f"{name}-{i}", f"Song {i}", name, "", None,
                                                 300, "theirs", genre, 1995)
                candidate["score"] = score
                state["pool"].append(candidate)
    state["targets"]["theirs"] = {"genre:Alternative Rock": alt_share, "genre:Indie Rock": 1 - alt_share,
                                  "era:1990": 1, "era:2000": 0}
    road_trip.finish_draft(state, store, 1000)
    assert state["stats"]["minutes"] == 120
    assert {c["decade"] for c in state["picked"]} == {"1990"}
    for genre, share in (("Alternative Rock", alt_share), ("Indie Rock", 1 - alt_share)):
        rows = [c for c in state["picked"] if c["genre"] == genre]
        assert len(rows) == round(24 * share)
        counts = Counter(c["artist"] for c in rows)
        named = sum(counts[a] for a in artists[genre])
        assert named == min(round(len(rows) * 2 / 3), seed_tracks * len(artists[genre]))
        if genre == "Alternative Rock":
            assert abs(counts["Weezer"] - counts["Staind"]) <= 1
    if seed_tracks > 1:
        index = next(i for i, c in enumerate(state["picked"]) if c["artist"] in state["inputs"]["artists"])
        old_id = state["picked"][index]["video_id"]
        road_trip.reroll_slot(state, store, index, 1000)
        assert state["picked"][index]["video_id"] != old_id
        assert state["picked"][index]["artist"] in state["inputs"]["artists"]


@pytest.mark.parametrize("shares, targets, expected", [
    ([.103, .207, .414, .276], [1, .25, .53, .35], [47, 12, 25, 16]),
    ([.103, .207, .414, .276], [1, None, None, None], [100, 0, 0, 0]),
    ([.1, .75, .25], [.35, None, None], [35, 49, 16]),
    ([.333, .333, .333], [None, None, None], [34, 33, 33]),
    ([1, 0, .4], [1, 0, None], [100, 0, 0]),
    ([0, 0, 0], [None, None, None], [34, 33, 33]),
])
def test_loading_a_saved_balance_uses_one_100_percent_budget(shares, targets, expected):
    state = {"axes": {"theirs": [
        {"key": f"genre:{i}", "kind": "genre", "name": str(i), "share": share, "target": target}
        for i, (share, target) in enumerate(zip(shares, targets))]},
        "targets": {"theirs": {f"genre:{i}": value for i, value in enumerate(targets) if value is not None}}}
    road_trip.normalized(state)
    rows = state["axes"]["theirs"]
    assert [row["balance_pct"] for row in rows] == expected
    assert sum(row["balance_pct"] for row in rows) == 100
    assert [row["share"] for row in rows] == shares  # actual track shares are not UI targets
    assert sum(state["targets"]["theirs"].values()) <= 1 + 1e-9
    road_trip.normalized(state)
    assert [row["balance_pct"] for row in rows] == expected  # no drift on repeated loads


def test_sliding_a_genre_down_reaches_the_requested_share(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    axis = "era:1990"
    assert _share(state, "mine", axis) > 0.2

    road_trip.set_share(state, "mine", axis, 0.1, store)

    assert abs(_share(state, "mine", axis) - 0.1) <= 0.12    # steered to roughly what was asked
    assert _share(state, "mine", axis) < 0.25                 # and clearly down from where it was
    assert next(a["target"] for a in state["axes"]["mine"] if a["key"] == axis) == 0.1


def test_sliding_a_genre_to_zero_removes_it_and_it_can_come_back(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=5)
    assert any(r["source"] == "theirs" for r in state["picked"])

    axis = next(a["key"] for a in state["axes"]["theirs"] if a["kind"] == "genre")
    road_trip.set_share(state, "theirs", axis, 0.0, store)

    assert not any(r["source"] == "theirs" for r in state["picked"])
    assert axis in {a["key"] for a in state["axes"]["theirs"]}   # the slider stays, so it can come back

    road_trip.clear_share(state, "theirs", axis, store)
    assert any(r["source"] == "theirs" for r in state["picked"])


def _mixed_library(monkeypatch, store, rock=4, other=36):
    """A collection deliberately thin on rock: 4 of 40, i.e. 10%. Play counts run 0..n, so the
    lowest-numbered song of each genre is the least played."""
    songs = [{"key": f"kr{i}", "video_id": f"r{i}", "title": f"Rock {i}",
              "artist": f"Rock Artist {i}", "album": "", "thumbnail": None, "duration": 300,
              "genre": "Rock", "year": 1990, "liked": False} for i in range(rock)]
    songs += [{"key": f"kp{i}", "video_id": f"p{i}", "title": f"Pop {i}",
               "artist": f"Pop Artist {i}", "album": "", "thumbnail": None, "duration": 300,
               "genre": "Pop", "year": 2000, "liked": False} for i in range(other)]
    monkeypatch.setattr(store, "library_songs", lambda: songs)
    monkeypatch.setattr(store, "play_counts", lambda: {s["key"]: i for i, s in enumerate(songs)})
    monkeypatch.setattr(road_trip, "other_input_songs", lambda *a, **k: ([], []))


def _genre_share(state, genre):
    mine = [r for r in state["picked"] if r["source"] == "mine"]
    return sum(1 for r in mine if r["genre"] == genre) / max(1, len(mine))


def test_asking_for_40_percent_rock_gets_40_percent_of_the_playlist(store, monkeypatch):
    """The bar is a share of the PLAYLIST, not of the pool. A quota that was only a ceiling let the
    draw hand back whatever proportion the pool happened to hold (ask 40, get 13); it has to be a
    floor too, filled first."""
    _mixed_library(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    assert _genre_share(state, "Rock") < 0.2         # the pool is thin on rock to start with

    road_trip.set_share(state, "mine", "genre:Rock", 0.4, store)

    assert 0.3 <= _genre_share(state, "Rock") <= 0.5
    assert next(a["share"] for a in state["axes"]["mine"] if a["key"] == "genre:Rock") >= 0.3


def test_forcing_a_genre_up_does_not_shrink_the_playlist(store, monkeypatch):
    """...and it must not cost you the rest of the playlist: capping everything else at 60% while
    rock can only reach 10% is what left a 5-song side."""
    _mixed_library(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    before = state["stats"]["own_count"]

    road_trip.set_share(state, "mine", "genre:Rock", 0.4, store)

    assert state["stats"]["own_count"] >= before - 1


def test_a_barely_played_song_is_still_reachable_when_its_genre_is_asked_for(store, monkeypatch):
    """Nothing is held back in a sample: pin a genre high and the least-played songs of it get
    picked, because the whole collection is the pool."""
    _mixed_library(monkeypatch, store, rock=12, other=28)
    state = road_trip.build_draft(store, FakeClient(),
                                  _recipe(own_pct=100, familiarity_pct=100), 1000.0, seed=5)

    road_trip.set_share(state, "mine", "genre:Rock", 1.0, store)

    rock = [r for r in state["picked"] if r["genre"] == "Rock"]
    assert len(rock) == len(state["picked"])              # the whole side is rock
    # r0 is the least-played rock song in the collection; a favorites-leaning draw still reaches it
    # once rock is the only thing allowed, rather than running out and shortening the playlist.
    assert len(rock) > 4


def test_one_genre_gets_one_bar_whatever_spelled_it(store, monkeypatch):
    """The same genre arrives as "Alternative Rock" (Last.fm), "alternative rock" (typed into the
    recipe) and "ALT ROCK". Three bars would each hold a quota the other two ignore."""
    assert (road_trip._canon_genre("alternative rock") == road_trip._canon_genre("ALT ROCK")
            == road_trip._canon_genre("Alternative Rock") == "Alternative Rock")
    assert road_trip._canon_genre("bagpipe funk") == "Bagpipe Funk"    # unknown: just tidied
    assert road_trip._canon_genre("  ") == ""


def test_a_bar_the_pool_can_no_longer_fill_goes_away(store, monkeypatch):
    """Sticky isn't forever. A bar left over from a pool that has since been re-drawn can never be
    satisfied again, so it stops taking up space - unless it's pinned, which is a standing request."""
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    state["axes"]["mine"].append({"key": "genre:Ghost", "kind": "genre", "name": "Ghost",
                                  "share": 0.0, "target": None})
    state["axes"]["mine"].append({"key": "genre:Pinned Ghost", "kind": "genre",
                                  "name": "Pinned Ghost", "share": 0.0, "target": 0.5})
    state["targets"]["mine"]["genre:Pinned Ghost"] = 0.5

    road_trip.repick(state, store)

    keys = {a["key"] for a in state["axes"]["mine"]}
    assert "genre:Ghost" not in keys
    assert "genre:Pinned Ghost" in keys


def test_era_bars_stay_in_chronological_order(store, monkeypatch):
    """Decades are a number line. A decade that only turns up on a later re-pick still has to slot
    into place rather than land on the end."""
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    eras = [a["name"] for a in state["axes"]["mine"] if a["kind"] == "era"]
    assert eras == sorted(eras)

    # A decade discovered later (a bar that wasn't in the first draw) is inserted, not appended.
    state["axes"]["mine"] = [a for a in state["axes"]["mine"] if a["key"] != "era:2000"]
    road_trip.repick(state, store)

    eras = [a["name"] for a in state["axes"]["mine"] if a["kind"] == "era"]
    assert eras == sorted(eras) and "2000" in eras


def test_era_slider_reweights_years(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    eras = {a["key"] for a in state["axes"]["mine"] if a["kind"] == "era"}
    assert eras == {"era:1990", "era:2000", "era:2010"}

    road_trip.set_share(state, "mine", "era:1990", 0.0, store)

    assert all(r["decade"] != "1990" for r in state["picked"])


def test_reroll_swaps_one_slot_and_leaves_the_rest_alone(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=9)
    before = list(state["picks"])
    before_src = [r["source"] for r in state["picked"]]

    road_trip.reroll_slot(state, store, 2)

    after = list(state["picks"])
    assert len(after) == len(before)
    assert after[:2] == before[:2] and after[3:] == before[3:]      # only slot 2 moved
    assert after[2] != before[2]
    assert state["picked"][2]["source"] == before_src[2]            # replaced like with like
    assert before[2] in state["banned"]                             # and it doesn't come back

    road_trip.reroll_slot(state, store, 2)
    assert state["picks"][2] not in (before[2], after[2])


def test_reroll_drops_the_slot_when_nothing_is_left_to_offer(store, monkeypatch):
    _stub_pools(monkeypatch, store, own_count=6, other_count=6)
    state = road_trip.build_draft(store, FakeClient(), _recipe(target_minutes=60), 1000.0, seed=9)
    picks = len(state["picks"])

    for _ in range(picks):                       # cross out slot 0 until the pool is exhausted
        road_trip.reroll_slot(state, store, 0)

    assert len(state["picks"]) < picks


def test_draft_tracks_are_ordered_playable_rows(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=4)

    tracks = road_trip.draft_tracks(state)

    assert [t["video_id"] for t in tracks] == state["picks"]
    assert all(t["title"] and t["artist"] and t["duration"] for t in tracks)


# --------------------------------------------------------------------------- ordering (journeys)

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
