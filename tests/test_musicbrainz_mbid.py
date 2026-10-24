from yt_playlist.providers import musicbrainz


def test_recording_mbid_returns_top_match(monkeypatch):
    def fake_get(path, params):
        assert path == "recording"
        return {"recordings": [
            {"id": mbid, "title": "Song", "artist-credit": [{"name": "Artist"}]}
            for mbid in ("mbid-top", "mbid-other")]}
    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    assert musicbrainz.recording_mbid("Song", "Artist") == "mbid-top"


def test_recording_mbid_none_on_no_match(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get", lambda path, params: {"recordings": []})
    assert musicbrainz.recording_mbid("Song", "Artist") is None


def test_recording_mbid_retries_stripped_title(monkeypatch):
    calls = []
    def fake_get(path, params):
        calls.append(params["query"])
        # first call (full title) returns nothing; second (stripped) returns a match
        if len(calls) == 1:
            return {"recordings": []}
        return {"recordings": [{"id": "mbid-stripped", "title": "No Quarter",
                                "artist-credit": [{"name": "Artist"}]}]}
    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    assert musicbrainz.recording_mbid("No Quarter (Remaster)", "Artist") == "mbid-stripped"
    assert len(calls) == 2


def test_recording_mbid_returns_none_on_get_exception(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError("timeout")))
    assert musicbrainz.recording_mbid("Song", "Artist") is None


def test_unrelated_recordings_cannot_supply_year_mbid_or_cover_releases(monkeypatch):
    recordings = [
        {"id": "wrong-artist", "title": "You & Me", "first-release-date": "1952",
         "artist-credit": [{"name": "Someone Else"}], "releases": [{"id": "wrong-cover"}]},
        {"id": "wrong-title", "title": "Another Song", "first-release-date": "2005",
         "artist-credit": [{"name": "Deco (BE)"}], "releases": [{"id": "wrong-cover-2"}]},
    ]
    monkeypatch.setattr(musicbrainz, "_get", lambda path, params: {"recordings": recordings})
    assert musicbrainz.enrich_full("You & Me", "Deco (BE)") == (None, None, None, [], [])


def test_collaboration_and_matching_reissues_keep_correct_year(monkeypatch):
    credit = [{"name": "MAARVN", "joinphrase": " & "},
              {"name": "Deco (BE)", "artist": {"name": "Deco (BE)"}}]
    recordings = [
        {"id": "wrong", "title": "You & Me", "first-release-date": "1952",
         "artist-credit": [{"name": "Someone Else"}], "releases": [{"id": "wrong-cover"}]},
        {"id": "right", "title": "You & Me (Remastered)", "first-release-date": "2026",
         "artist-credit": credit, "releases": [{"id": "right-cover"}]},
        {"id": "original", "title": "You & Me", "first-release-date": "2025-08-01",
         "artist-credit": credit, "releases": [{"id": "original-cover"}]},
    ]
    def fake_get(path, params):
        if path == "recording":
            return {"recordings": recordings}
        assert path == "recording/right"
        return {"genres": [{"name": "house", "count": 1}]}
    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    assert musicbrainz.enrich_full("You & Me", "Deco (BE)") == (
        "House", "2025", "right", ["right-cover", "original-cover"], [])
    assert musicbrainz.recording_mbid("You & Me", "MAARVN & Deco (BE)") == "right"


def test_artist_alias_is_a_valid_credit(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_search_recordings", lambda *args: [
        {"id": "right", "title": "Song", "artist-credit": [
            {"name": "Credited Name", "artist": {"name": "Canonical Name",
                                                     "aliases": [{"name": "Alias"}]}}]}])
    assert musicbrainz.recording_mbid("Song", "Alias") == "right"


def test_retry_does_not_drop_meaningful_title_qualifier(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_search_recordings", lambda *args: [
        {"id": "wrong", "title": "Song", "artist-credit": [{"name": "Artist"}]}])
    assert musicbrainz.recording_mbid("Song (Part II)", "Artist") is None


def test_live_remix_and_radio_edit_are_not_studio_matches(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_search_recordings", lambda *args: [
        {"id": "studio", "title": "Song", "artist-credit": [{"name": "Artist"}]}])
    for title in ("Song (Live)", "Song (Club Remix)", "Song (Radio Edit)"):
        assert musicbrainz.recording_mbid(title, "Artist") is None


def test_full_enrichment_retains_isrc_candidates(monkeypatch):
    def fake_get(path, params):
        if path == "recording":
            return {"recordings": [{"id": "recording", "title": "Song",
                    "artist-credit": [{"name": "Artist"}]}]}
        assert path == "recording/recording"
        assert "isrcs" in params["inc"]
        return {"isrcs": ["GB1234567890", "GB0987654321"]}
    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    assert musicbrainz.enrich_full("Song", "Artist")[4] == [
        "GB1234567890", "GB0987654321"]
