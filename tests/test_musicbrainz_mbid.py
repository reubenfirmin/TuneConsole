from yt_playlist.providers import musicbrainz


def test_recording_mbid_returns_top_match(monkeypatch):
    def fake_get(path, params):
        assert path == "recording"
        return {"recordings": [{"id": "mbid-top", "artist-credit": []},
                               {"id": "mbid-other", "artist-credit": []}]}
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
        return {"recordings": [{"id": "mbid-stripped"}]}
    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    assert musicbrainz.recording_mbid("No Quarter (Remaster)", "Artist") == "mbid-stripped"
    assert len(calls) == 2


def test_recording_mbid_returns_none_on_get_exception(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError("timeout")))
    assert musicbrainz.recording_mbid("Song", "Artist") is None
