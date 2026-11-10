from tests.repository_mock_support import no_database, transient_repository_store
from yt_playlist.util import genre_map


def test_subgenre_resolution_keeps_canonical_labels_separate_from_families():
    assert genre_map.family("minimal techno") == "techno"
    assert genre_map.subgenre("minimal techno") == "minimal techno"
    assert genre_map.subgenre("techno") == "techno"          # a tag that is its own family name
    assert genre_map.subgenre("Minimal Techno") == "minimal techno"
    assert genre_map.subgenre("TECHNO") == "techno"
    assert genre_map.subgenre("not a real genre xyz") is None
    assert genre_map.subgenre("") is None


def test_family_members_include_known_subgenres_and_unknown_family_is_empty():
    members = genre_map.subgenres_of("techno")
    assert "minimal techno" in members
    assert "dub techno" in members
    assert "detroit techno" in members
    assert genre_map.subgenres_of("not a real family xyz") == []


def test_subgenre_lean_is_independent_of_family(no_database):
    from yt_playlist.rec import scoring
    s = transient_repository_store(
        rec_query={'track_genres': {'a|x': 'minimal techno', 'b|y': 'dub techno'}},
        rec_model={'get_leans': {'genre:minimal techno': 1.6}},
        genres={'feature_axes': {'minimal techno': ('techno', 'minimal techno'),
                                 'dub techno': ('techno', 'dub techno')}},
    )
    mult = scoring._axis_weights_for(s, ["a|x", "b|y"], now=1000.0)
    assert mult["a|x"] > mult["b|y"]   # only the minimal-techno track is lifted; both share family techno
