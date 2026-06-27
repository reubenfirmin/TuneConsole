from yt_playlist.util import genre_map


def test_subgenre_distinct_from_family():
    assert genre_map.family("minimal techno") == "techno"
    assert genre_map.subgenre("minimal techno") == "minimal techno"
    assert genre_map.subgenre("techno") == "techno"          # a tag that is its own family name


def test_subgenres_of_family_lists_members():
    assert "minimal techno" in genre_map.subgenres_of("techno")


def test_subgenre_unknown_is_none():
    assert genre_map.subgenre("not a real genre xyz") is None


def test_subgenre_case_insensitive():
    assert genre_map.subgenre("Minimal Techno") == "minimal techno"
    assert genre_map.subgenre("TECHNO") == "techno"


def test_subgenres_of_unknown_family_is_empty():
    assert genre_map.subgenres_of("not a real family xyz") == []


def test_subgenres_of_contains_known_members():
    members = genre_map.subgenres_of("techno")
    assert "dub techno" in members
    assert "detroit techno" in members


def test_subgenre_empty_is_none():
    assert genre_map.subgenre("") is None


def test_subgenre_lean_is_independent_of_family():
    from yt_playlist.core.store import Store
    from yt_playlist.rec import recommend
    s = Store(":memory:"); s.init_schema()
    a = s.upsert_track("v1", "a", "x", None, None); s.set_track_genre(a, "minimal techno")
    b = s.upsert_track("v2", "b", "y", None, None); s.set_track_genre(b, "dub techno")
    s.set_lean("genre:minimal techno", 1.6, 1000.0)
    mult = recommend._axis_weights_for(s, ["a|x", "b|y"], now=1000.0)
    assert mult["a|x"] > mult["b|y"]   # only the minimal-techno track is lifted; both share family techno
