from yt_playlist.rec import discover, genre_map, rec_params


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
