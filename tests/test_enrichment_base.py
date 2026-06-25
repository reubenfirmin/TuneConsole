"""Field comparison + conflict detection (provider-agnostic)."""
from yt_playlist.providers.base import EnrichmentResult, Discrete, Numeric, detect_conflicts


def test_discrete_agreement_is_case_insensitive():
    d = Discrete()
    assert d.agree("Rock", "rock")
    assert not d.agree("Rock", "Indie Rock")


def test_numeric_agreement_within_tolerance():
    n = Numeric(2.0)
    assert n.agree(120.0, 121.5)      # within 2
    assert not n.agree(120.0, 124.0)  # beyond 2


def R(provider, **fields):
    return EnrichmentResult(provider, fields)


def test_no_conflict_when_providers_agree():
    res = [R("musicbrainz", genre="Rock", year="1994"),
           R("discogs", genre="rock", year="1994")]   # same (case-insensitive)
    assert detect_conflicts(res) == {}


def test_conflict_on_disagreeing_genre_lists_all_candidates():
    res = [R("musicbrainz", genre="Electronic"),
           R("discogs", genre="Art Pop"),
           R("lastfm", genre="Trip Hop")]
    c = detect_conflicts(res)
    assert set(c) == {"genre"}
    assert [x["value"] for x in c["genre"]] == ["Electronic", "Art Pop", "Trip Hop"]


def test_numeric_within_tolerance_does_not_conflict():
    res = [R("deezer", bpm=120.0), R("acousticbrainz", bpm=121.0)]
    assert detect_conflicts(res) == {}


def test_numeric_beyond_tolerance_conflicts():
    res = [R("deezer", bpm=120.0), R("acousticbrainz", bpm=140.0)]
    assert set(detect_conflicts(res)) == {"bpm"}


def test_single_value_is_not_a_conflict():
    res = [R("musicbrainz", genre="Rock"), R("discogs")]   # discogs found nothing
    assert detect_conflicts(res) == {}


def test_empty_strings_ignored():
    res = [R("musicbrainz", genre="Rock"), R("discogs", genre=""), R("lastfm", genre="Rock")]
    assert detect_conflicts(res) == {}
