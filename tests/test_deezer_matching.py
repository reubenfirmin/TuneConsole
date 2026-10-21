"""Provider metadata cannot enter the catalogue on search rank alone."""
import pytest

from yt_playlist.providers import deezer


def _track(id=1, title="Song", artist="Artist", **extra):
    return {"id": id, "title": title, "artist": {"name": artist}, "bpm": 120,
            "rank": 100, "gain": -5, "duration": 200, "release_date": "2000-01-01",
            "album": {"cover_xl": "cover"}, **extra}


def _stub(monkeypatch, tracks, *, search=None):
    calls = []
    def get(url):
        calls.append(url)
        if "/search/track" in url:
            return {"data": search if search is not None else tracks}
        return next(t for t in tracks if url.endswith(f"/track/{t['id']}"))
    monkeypatch.setattr(deezer, "_get_json", get)
    return calls


@pytest.mark.parametrize("candidate", [
    _track(artist="Someone else"), _track(title="Song (Live)"),
    _track(title="Song - Radio Edit"), _track(title="Song (Remix)"),
    _track(title_version="Live"), _track(artist=""), _track(title=""),
])
def test_mismatched_identity_yields_no_metadata_or_art(monkeypatch, candidate):
    _stub(monkeypatch, [candidate])
    assert all(v is None for v in deezer.lookup("Song", "Artist").values())


@pytest.mark.parametrize("title", ["Song (Live)", "Song (Remix)", "Song (Acoustic Version)"])
def test_studio_result_cannot_satisfy_a_qualified_request(monkeypatch, title):
    _stub(monkeypatch, [_track()])
    assert deezer.lookup(title, "Artist")["bpm"] is None


def test_valid_later_candidate_and_benign_formatting(monkeypatch):
    calls = _stub(monkeypatch, [_track(title="Cafe", artist="Wrong artist"),
        _track(2, "Café! (2011 Remastered)", "Björk")])
    assert deezer.lookup("Cafe", "Bjork")["bpm"] == 120
    assert "limit=10" in calls[0]


def test_separate_version_field_keeps_meaningful_qualifiers(monkeypatch):
    _stub(monkeypatch, [_track(title_version="Live")])
    assert deezer.lookup("Song (Live)", "Artist")["bpm"] == 120
    _stub(monkeypatch, [_track(title_version="2011 Remastered")])
    assert deezer.lookup("Song", "Artist")["bpm"] == 120


def test_secondary_credited_artist_is_verified_from_track_details(monkeypatch):
    _stub(monkeypatch, [_track(artist="Lead", contributors=[{"name": "Lead"}, {"name": "Artist"}])])
    assert deezer.lookup("Song", "Artist")["bpm"] == 120


def test_full_detail_identity_is_rechecked(monkeypatch):
    _stub(monkeypatch, [_track(artist="Impostor")], search=[_track()])
    assert deezer.lookup("Song", "Artist")["art"] is None


def test_distinct_candidates_without_strong_evidence_are_ambiguous(monkeypatch):
    _stub(monkeypatch, [_track(), _track(2)])
    assert deezer.lookup("Song", "Artist")["bpm"] is None


def test_shared_explicit_recording_identifier_allows_duplicate_releases(monkeypatch):
    _stub(monkeypatch, [_track(isrc="US-ABC-00-00001"), _track(2, isrc="USABC0000001")])
    assert deezer.lookup("Song", "Artist")["bpm"] == 120


def test_known_duration_rejects_a_different_recording_and_selects_later_candidate(monkeypatch):
    _stub(monkeypatch, [_track(duration=300), _track(2)])
    assert deezer.lookup("Song", "Artist", duration=202)["duration"] == 200


def test_unknown_duration_cannot_disambiguate(monkeypatch):
    _stub(monkeypatch, [_track(duration=None), _track(2)])
    assert deezer.lookup("Song", "Artist", duration=200)["bpm"] is None


def test_known_isrc_requires_matching_evidence(monkeypatch):
    _stub(monkeypatch, [_track(isrc="OTHER"), _track(2, isrc="USABC0000001")])
    assert deezer.lookup("Song", "Artist", isrc=["US-ABC-00-00001"])["bpm"] == 120
    assert deezer.lookup("Song", "Artist", isrc="MISSING")["bpm"] is None


def test_unknown_requested_identity_does_not_query(monkeypatch):
    calls = _stub(monkeypatch, [_track()])
    assert deezer.lookup("Song", "")["bpm"] is None
    assert calls == []


def test_unread_candidate_cannot_establish_uniqueness(monkeypatch):
    def get(url):
        if "/search/track" in url:
            return {"data": [_track(), _track(2)]}
        if url.endswith("/track/1"):
            return _track()
        raise TimeoutError()
    monkeypatch.setattr(deezer, "_get_json", get)
    assert deezer.lookup("Song", "Artist")["bpm"] is None


def test_waterfall_uses_known_duration_before_persisting_any_fact(monkeypatch, store):
    entry = store.upsert_track("video", "Song", "Artist", None, 300)
    _stub(monkeypatch, [_track()])
    assert deezer.probe(store.catalog_subject(entry), store).fields == {}
