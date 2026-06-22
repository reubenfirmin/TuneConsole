from yt_playlist.rec import genre_map as gm


def test_related_genres_share_a_family():
    assert gm.family("Tech Trance") == gm.family("Trance")
    assert gm.family("Deep House") == gm.family("Tech House")
    assert gm.family("Acid House") == gm.family("House")
    assert gm.family("Death Metal") == gm.family("Thrash Metal")


def test_distance_orders_related_below_unrelated():
    assert gm.distance("Trance", "Tech Trance") == 0.0
    assert 0.0 < gm.distance("Trance", "Techno") < 1.0     # adjacent families
    assert gm.distance("Trance", "Hard Rock") == 1.0       # unrelated


def test_unknown_genre_is_singleton():
    assert gm.family("Totally Made Up Genre").startswith("other:")
    assert gm.distance("Totally Made Up Genre", "Trance") == 1.0
    assert gm.distance("Totally Made Up Genre", "Totally Made Up Genre") == 0.0


def test_energy_orders_families_and_defaults():
    from yt_playlist.rec import genre_map
    assert genre_map.energy("ambient") < genre_map.energy("metal")   # mellow < intense
    assert 0.0 <= genre_map.energy("techno") <= 1.0
    assert genre_map.energy("") == 0.5                                # untagged -> neutral mid
    assert genre_map.energy("not-a-real-genre") == 0.5               # unknown -> neutral mid
