from yt_playlist.util import genre_map
from yt_playlist.rec import discover, rec_params


def _mute(store, genre):
    store.set_weight(f"genre:{genre_map.family(genre)}", 0.0,
                     lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)


def _favor(store, genre):
    store.set_weight(f"genre:{genre_map.family(genre)}", 2.0,
                     lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)


def test_pick_artists_excludes_muted_family(store):
    store.upsert_discovered_artist("TechA", 0.9, [], [], None, 1.0, genre="Techno")
    store.upsert_discovered_artist("HouseB", 0.5, [], [], None, 1.0, genre="House")
    _mute(store, "Techno")
    names = [p["artist"] for p in discover.pick_discovered_artists(store, 5, now=1.0)]
    assert "TechA" not in names and "HouseB" in names


def test_pick_artists_favored_outranks_neutral_at_equal_score(store):
    store.upsert_discovered_artist("FavHouse", 0.5, [], [], None, 1.0, genre="House")
    store.upsert_discovered_artist("NeutralAmb", 0.5, [], [], None, 1.0, genre="Ambient")
    _favor(store, "House")
    picked = discover.pick_discovered_artists(store, 2, now=1.0)
    assert picked[0]["artist"] == "FavHouse"     # same base score, favored family wins


def test_pick_artists_untagged_survives(store):
    store.upsert_discovered_artist("Unknown", 0.7, [], [], None, 1.0)   # no genre
    _mute(store, "Techno")
    names = [p["artist"] for p in discover.pick_discovered_artists(store, 5, now=1.0)]
    assert "Unknown" in names     # untagged is neutral, never excluded


def test_pick_albums_excludes_muted_family(store):
    store.upsert_discovered_album("b1", "TechArtist", "Album T", "2020", None, 1.0, genre="Techno")
    store.upsert_discovered_album("b2", "HouseArtist", "Album H", "2020", None, 1.0, genre="House")
    _mute(store, "Techno")
    titles = [a["title"] for a in discover.pick_discovered_albums(store, 5, now=1.0)]
    assert "Album T" not in titles and "Album H" in titles


def test_large_album_and_artist_pools_reuse_family_feedback(store, monkeypatch):
    albums = [{"browse_id": f"b{i}", "artist": f"A{i}", "year": "2026", "genre": "Deep House"}
              for i in range(300)]
    artists = [{"artist": f"A{i}", "score": 0.5, "genre": "House"} for i in range(300)]
    monkeypatch.setattr(store, "get_discovered_albums", lambda: albums)
    monkeypatch.setattr(store, "get_discovered_artists", lambda: artists)
    calls = []
    monkeypatch.setattr(discover.recommend, "discovery_facet_weight",
                        lambda s, family, now: calls.append(family) or 1.0)
    assert len(discover.pick_discovered_albums(store, 15, now=1)) == 15
    assert len(discover.pick_discovered_artists(store, 10, now=1)) == 10
    assert calls == ["house", "house"]
