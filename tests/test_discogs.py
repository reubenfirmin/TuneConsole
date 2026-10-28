"""Discogs search hits must describe the requested artist and release before supplying metadata."""
import pytest

from yt_playlist.providers import discogs


def test_unrelated_search_hit_cannot_supply_earliest_year(monkeypatch):
    # Actual search results for Critical Choice: the old minimum over all hits mixed its genre
    # with the year of the fourth result, an unrelated Schumann recording.
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"title": "Critical Choice - For Those Who Dance", "year": "2026",
         "genre": ["Electronic"], "style": ["Psy-Trance", "Progressive Trance"]},
        {"title": "Terre Thaemlitz - Snowflakes & Dog Whistles", "year": "2024",
         "genre": ["Electronic"], "style": ["Ambient"]},
        {"title": "Infernal Legions Of Mordor - Discography Boxed Set", "year": "2024",
         "genre": ["Rock"], "style": ["Noisecore"]},
        {"title": "Schumann* - Alexander Brailowsky - Carnaval, Op. 9 / Fantasia In C, Op. 17",
         "year": "1952", "genre": ["Classical"], "style": ["Romantic"]},
    ])
    assert discogs.enrich("For Those Who Dance", "Critical Choice") == ("Psytrance", "2026")


@pytest.mark.parametrize("other_title", ["Other Artist - For Those Who Dance",
                                       "Critical Choice - Different Release"])
def test_wrong_artist_or_title_cannot_supply_genre_or_year(monkeypatch, other_title):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"title": other_title, "year": "1952", "genre": ["Classical"]},
        {"title": "Critical Choice - For Those Who Dance", "year": "2026",
         "style": ["Psy-Trance"]},
    ])
    assert discogs.enrich("For Those Who Dance", "Critical Choice") == ("Psytrance", "2026")


def test_matching_reissues_keep_earliest_year_and_normalize_artist_suffix(monkeypatch):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"title": "Björk (2) - Hyperballad (Remastered)", "year": "2015", "genre": ["Electronic"]},
        {"title": "Bjork - Hyperballad", "year": "1995", "genre": ["Electronic"]},
        {"title": "Bjork - A Different Song", "year": "1977", "genre": ["Pop"]},
    ])
    assert discogs.enrich("Hyperballad", "Björk") == ("Electronic", "1995")


def test_unmatched_search_results_are_a_miss(monkeypatch):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"title": "Schumann - Carnaval", "year": "1952", "genre": ["Classical"]},
        {"year": "1900", "genre": ["Rock"]},
    ])
    assert discogs.enrich("For Those Who Dance", "Critical Choice") == (None, None)


def test_matching_release_without_year_does_not_borrow_one(monkeypatch):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"title": "Critical Choice - For Those Who Dance", "style": ["Psy-Trance"]},
        {"title": "Schumann - Carnaval", "year": "1952", "genre": ["Classical"]},
    ])
    assert discogs.enrich("For Those Who Dance", "Critical Choice") == ("Psytrance", None)


def test_deco_unrelated_results_are_not_metadata_options(monkeypatch):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"id": 1, "title": "IAMX - Dogmatic Infidel Comedown OK", "year": "2010"},
        {"id": 2, "title": "Lana Del Rey - Honeymoon", "year": "2015"},
        {"id": 3, "title": "David Crowder*Band - Sunsets & Sushi", "year": "2005",
         "genre": ["Rock"], "style": ["Alternative Rock"]},
    ])
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Unrelated artists do not need release lookups")
    monkeypatch.setattr(discogs, "_get", unexpected_lookup)
    assert discogs.enrich("You & Me", "Deco (BE)") == (None, None)


@pytest.mark.parametrize("compilation", [False, True])
def test_album_hit_must_contain_the_requested_artists_track(monkeypatch, compilation):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"id": 1, "title": "Björk - Unrelated Album", "year": "1980", "genre": ["Pop"]},
        {"id": 2, "title": ("Various" if compilation else "Björk") + " - Post",
         "year": "1995", "genre": ["Electronic"]},
    ])
    def fake_get(path, params, tok, **kwargs):
        if path == "releases/1":
            return {"artists": [{"name": "Björk"}], "tracklist": [{"title": "Another Song"}]}
        assert path == "releases/2"
        return {"artists": [{"name": "Various" if compilation else "Björk"}], "tracklist": [
            {"title": "Hyperballad", **({"artists": [{"name": "Björk"}]} if compilation else {})}]}
    monkeypatch.setattr(discogs, "_get", fake_get)
    assert discogs.enrich("Hyperballad", "Björk") == ("Electronic", "1995")


def test_release_lookup_failure_does_not_validate_album(monkeypatch):
    monkeypatch.setattr(discogs, "_search", lambda query, tok: [
        {"id": 1, "title": "Björk - Post", "year": "1995", "genre": ["Electronic"]}])
    def fail(*args, **kwargs):
        raise OSError("timeout")
    monkeypatch.setattr(discogs, "_get", fail)
    assert discogs.enrich("Hyperballad", "Björk") == (None, None)
