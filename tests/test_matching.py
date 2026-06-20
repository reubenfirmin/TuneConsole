# tests/test_matching.py
from yt_playlist.matching import normalize, identity_key, fuzzy_ratio

def test_normalize_strips_noise():
    assert normalize("Wonderwall (Remastered 2014)") == "wonderwall"
    assert normalize("Song (feat. Someone)") == "song"
    assert normalize("Café  Del   Mar!!!") == "cafe del mar"

def test_identity_key_combines_title_artist():
    assert identity_key("Wonderwall (Remastered)", "Oasis") == "wonderwall|oasis"

def test_normalize_strips_underscores():
    assert normalize("Some_Song_Title") == "some song title"
    assert identity_key("Lo-Fi_Beats", "DJ_X") == "lo fi beats|dj x"

def test_fuzzy_ratio_high_for_near_match():
    assert fuzzy_ratio("wonderwall oasis", "oasis wonderwall") > 0.9
    assert fuzzy_ratio("wonderwall", "completely different") < 0.5
