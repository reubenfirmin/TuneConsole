"""Library, artist and genre candidate acquisition without live providers."""
import pytest

from yt_playlist.rec import road_trip, road_trip_candidates as candidates
from yt_playlist.rec.road_trip_state import ROW_FIELDS
from tests.conftest import FakeClient
from tests.road_trip_support import _library, _recipe, no_network

# Captured before the autouse no_network fixture stubs it out, for the one test that exercises the
# real genre -> artists resolution rather than mocking past it.
_real_genre_artists = candidates.genre_artists


@pytest.mark.parametrize("kind", ["artist", "genre"])
def test_candidate_acquisition_failure_returns_empty_batch(monkeypatch, store, kind):
    client = FakeClient()

    def unavailable(*args, **kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(client, "search", unavailable)
    batch = candidates.acquire_input(client, store, candidates.InputRequest(kind, "Unavailable", 8))
    assert batch == candidates.CandidateBatch([], [])


def test_own_side_is_the_whole_collection_ranked_by_engagement(store, monkeypatch):
    """Your side is not a sample of your library, it IS your library - spread along how much you
    actually listen, which is what the favorites/deeper-cuts slider slides along."""
    _library(monkeypatch, store, [
        ("k_a", "v_a", "Deep Cut", "Artist A", 0, False),
        ("k_b", "v_b", "Favorite", "Artist B", 40, False),
        ("k_c", "v_c", "Liked But Unplayed", "Artist C", 0, True)])

    pool = candidates.own_candidates(store, 1000.0)

    assert {c["video_id"] for c in pool} == {"v_a", "v_b", "v_c"}
    assert all(c["source"] == "mine" for c in pool)
    by_id = {c["video_id"]: c for c in pool}
    assert by_id["v_b"]["fam"] > by_id["v_c"]["fam"] > by_id["v_a"]["fam"]
    assert by_id["v_c"]["engagement"] == candidates.LIKE_BONUS   # a like counts as listening


def test_own_side_honours_the_taste_models_exclusions(store, monkeypatch):
    _library(monkeypatch, store, [("k_a", "v_a", "Keep", "Artist A", 1, False),
                                  ("k_b", "v_b", "Dismissed", "Artist B", 1, False),
                                  ("k_c", "v_c", "Muted", "Muted Artist", 1, False)])
    monkeypatch.setattr(store.rec_model, "suppressed_keys", lambda surface, now: {"k_b"})
    monkeypatch.setattr(store.rec_model, "muted_artists", lambda: {"Muted Artist"})

    assert [c["video_id"] for c in candidates.own_candidates(store, 1000.0)] == ["v_a"]


def test_own_side_honours_recipe_genre_blacklist(store, monkeypatch):
    _library(monkeypatch, store, [("k_a", "v_a", "Keep", "Artist A", 1, False),
                                  ("k_b", "v_b", "Blocked", "Artist B", 1, False)])
    monkeypatch.setattr(store.rec_query, "keys_in_genre_selection", lambda genres: {"k_b"})

    state = {"blacklist_genres": ["Metal"]}
    assert [c["video_id"] for c in candidates.own_candidates(store, 1000.0, state)] == ["v_a"]


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

    songs = candidates.artist_songs(client, "Tame Impala")

    assert [s["video_id"] for s in songs] == ["v1", "v2"]
    assert songs[0]["artist"] == "Tame Impala"
    assert songs[0]["album"] == "Currents"
    assert songs[0]["duration"] == 245
    assert songs[1]["duration"] is None   # no duration_seconds on this row -> filled in later


def test_artist_songs_unresolvable_artist_returns_empty():
    assert candidates.artist_songs(FakeClient(search_results=[]), "Nobody Band") == []


def test_genre_songs_keeps_the_search_term_as_the_genre():
    client = FakeClient(search_results=[
        {"videoId": "g1", "title": "Genre Hit", "artists": [{"name": "Genre Artist"}],
         "album": {"name": "Genre Alb"}, "duration_seconds": 210, "thumbnails": []},
    ])

    songs = candidates._genre_songs(client, "synthpop")

    assert songs == [{"video_id": "g1", "title": "Genre Hit", "artist": "Genre Artist",
                      "album": "Genre Alb", "thumbnail": None, "duration": 210,
                      "genre": "synthpop"}]


def test_a_genre_resolves_to_its_top_artists_then_their_youtube_tracks(monkeypatch):
    """A genre input goes through the music databases, not a YouTube text search: ask who plays the
    genre, then ask YouTube for those artists' biggest tracks. Searching YouTube for a genre STRING
    returns mixes, karaoke and "top 100" uploads."""
    monkeypatch.setattr(candidates, "genre_artists",
                        lambda store, genre, decade=None, limit=12: ["Weezer", "Pixies"])
    monkeypatch.setattr(candidates, "artist_songs",
                        lambda client, name, limit=30: _fake_artist_songs(4)(None, name))
    searched = []
    monkeypatch.setattr(candidates, "_genre_songs",
                        lambda client, genre, limit=30: searched.append(genre) or [])

    rows, related = candidates.other_input_songs(None, "genre", "Alternative Rock", cap=8)

    assert {r["artist"] for r in rows} == {"Weezer", "Pixies"}
    assert all(r["genre"] == "Alternative Rock" for r in rows)   # tagged with what brought them in
    assert searched == [], "the raw genre search is a last resort, not the first move"


def test_a_genre_no_database_knows_falls_back_to_searching_youtube(monkeypatch):
    monkeypatch.setattr(candidates, "genre_artists", lambda store, genre, decade=None, limit=12: [])
    monkeypatch.setattr(candidates, "_genre_songs", lambda client, genre, limit=30: [
        {"video_id": "g1", "title": "Hit", "artist": "Someone", "album": "", "thumbnail": None,
         "duration": 200, "genre": genre}])

    rows, _ = candidates.other_input_songs(None, "genre", "bagpipe funk", cap=8)

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

    monkeypatch.setattr(candidates, "_artist_page", page)
    monkeypatch.setattr(candidates, "_facts", lambda title, artist: {
        "year": catalog[artist][0], "genre": catalog[artist][1], "popularity": 500, "duration": 240})
    monkeypatch.setattr(candidates, "artist_genre", lambda store, artist: catalog[artist][1])
    monkeypatch.setattr(candidates, "genre_artists", lambda *a, **kw: pytest.fail("Broad genre lookup"))
    monkeypatch.setattr(candidates, "_genre_songs", lambda *a, **kw: pytest.fail("Broad genre search"))

    state = road_trip.build_draft(store, FakeClient(), _recipe(
        own_pct=0, artists=["Weezer", "Staind"], genres=["Alternative Rock"]), 1000, seed=7)

    assert {c["artist"] for c in state["pool"]} == {"Weezer", "Staind", "Peer A", "Peer B"}
    axis = next(a for a in state["axes"]["theirs"] if a["key"] == "genre:Alternative Rock")
    assert axis["artists"] == ["Weezer", "Staind"]
    assert all(c["cluster_genre"] == "Alternative Rock" for c in state["pool"])

    # Older drafts can contain a broad genre pool. The next slider move replaces those results
    # with the same seed neighborhood, even if there are already enough generic tracks.
    for name in ("Unrelated", "Modern Revival"):
        candidate = candidates._candidate(name, name, name, "", None, 240, "theirs",
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

    monkeypatch.setattr(candidates, "_artist_page", page)
    monkeypatch.setattr(candidates, "_facts", lambda title, artist: {"popularity": 500})
    monkeypatch.setattr(candidates, "artist_genre", lambda store, artist: catalog[artist][0])
    monkeypatch.setattr(candidates, "genre_artists", lambda *a, **kw: pytest.fail("Broad genre lookup"))
    monkeypatch.setattr(candidates, "_genre_songs", lambda *a, **kw: pytest.fail("Broad genre search"))
    state = road_trip.build_draft(store, FakeClient(), _recipe(
        own_pct=0, artists=seeds, genres=["alt rock", "indie rock"]), 1000, seed=7)
    expected = {"Alternative Rock": ["Weezer", "Staind"], "Indie Rock": [seeds[1]]}
    assert {a["name"]: a["artists"] for a in state["axes"]["theirs"]
            if a["kind"] == "genre"} == expected

    # Reproduce cached cross-family cluster assignments and a removed artist's lingering genre.
    for candidate in state["pool"]:
        candidate["cluster_genre"] = "Alternative Rock"
    extra = candidates._candidate("old-peer", "Old peer track", "Alt Peer", "", None, 240,
                                 "theirs", "Alternative Rock", None)
    extra.update(cluster_genre="Post-Punk", input="Post-Punk", anchors=seeds)
    state["pool"].append(extra)
    for name, origin in (("Ben Folds", "Ben Folds Five"), ("Generic Singer", "Singer-Songwriter")):
        candidate = candidates._candidate(name, name, name, "", None, 240,
                                         "theirs", "Singer-Songwriter", None)
        candidate["input"] = origin
        state["pool"].append(candidate)
    for genre in ("Post-Punk", "Singer-Songwriter"):
        state["axes"]["theirs"].append({"key": "genre:" + genre, "kind": "genre",
                                        "name": genre, "share": .25})
    for axis in state["axes"]["theirs"]:
        axis.update(artists=seeds, target=.25)
        state["targets"]["theirs"][axis["key"]] = .25
    state["picked"] = [{k: c[k] for k in ROW_FIELDS} for c in state["pool"]]
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

    monkeypatch.setattr(candidates, "other_input_songs", songs)
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


def test_genre_artists_prefers_lastfm_and_uses_musicbrainz_for_a_decade(monkeypatch, store):
    monkeypatch.setattr(candidates.lastfm, "api_key", lambda store=None: "k")
    monkeypatch.setattr(candidates.lastfm, "tag_top_artists",
                        lambda tag, key, limit=25: ["Weezer", "Pixies"])
    mb_calls = []

    def mb(tag, decade=None, limit=25, *, store=None):
        assert store is expected_store
        mb_calls.append((tag, decade))
        return ["Pixies", "Interpol", "Various Artists"] if decade else ["Filler"]

    monkeypatch.setattr(candidates.musicbrainz, "tag_artists", mb)
    expected_store = store
    candidates._GENRE_ARTIST_CACHE.clear()

    assert _real_genre_artists(store, "Alternative Rock") == ["Weezer", "Pixies"]
    assert mb_calls == []                          # Last.fm answered; no need to ask further

    candidates._GENRE_ARTIST_CACHE.clear()
    got = _real_genre_artists(store, "Alternative Rock", decade="2000")

    assert mb_calls == [("Alternative Rock", "2000")]   # only MB can filter on a decade
    assert got[0] == "Pixies"          # both sources agree on Pixies, so it leads
    assert got[1:] == ["Weezer", "Interpol"]   # then their leftovers, interleaved
    assert "Various Artists" not in got         # not an artist; chasing it returns compilations


def _fake_artist_songs(count, prefix="ArtistA"):
    def fake(client_, name, limit=candidates.ARTIST_SONGS_LIMIT):
        return [{"video_id": f"{name}_{i}", "title": f"{name} Song {i}", "artist": name,
                 "album": "", "thumbnail": None, "duration": 300, "genre": ""}
                for i in range(count)]
    return fake


def test_artist_input_cap_scales_with_demand(monkeypatch):
    """A single named artist must be able to fill their whole half. A fixed per-artist cap is what
    made a 50% "theirs" recipe come back almost entirely "mine"."""
    monkeypatch.setattr(candidates, "_page_songs",
                        lambda page, name, limit=30: _fake_artist_songs(25)(None, name))
    monkeypatch.setattr(candidates, "_artist_page", lambda client, name: ({}, "UC1"))

    cap = candidates.other_cap(['ArtistA'], [], limit=20)
    batch = candidates.acquire_input(FakeClient(), None, candidates.InputRequest('artist', 'ArtistA', cap))

    assert len(batch.rows) == 20
    assert {row['artist'] for row in batch.rows} == {'ArtistA'}
