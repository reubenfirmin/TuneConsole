# tests/test_deezer.py
from yt_playlist.providers import deezer


def test_enrich_returns_metadata_dict(monkeypatch):
    def fake_get_json(url):
        if "/search/track" in url:
            return {"data": [{"id": 42, "title": "Song", "artist": {"name": "Artist"}}]}
        if "/track/42" in url:
            return {"id": 42, "bpm": 128.0, "gain": -7.0, "rank": 856376,
                    "album": {"id": 99}}
        if "/album/99" in url:
            return {"id": 99, "label": "Because Music"}
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(deezer, "_get_json", fake_get_json)
    feat = deezer.enrich("Song", "Artist")
    assert feat["bpm"] == 128.0
    assert feat["gain"] == -7.0
    assert feat["popularity"] == 856376
    assert feat["label"] == "Because Music"


def test_enrich_treats_zero_bpm_as_unknown(monkeypatch):
    def fake_get_json(url):
        if "/search/track" in url:
            return {"data": [{"id": 1}]}
        return {"id": 1, "bpm": 0}
    monkeypatch.setattr(deezer, "_get_json", fake_get_json)
    assert deezer.enrich("Song", "Artist")["bpm"] is None


def test_enrich_none_on_no_search_hit(monkeypatch):
    monkeypatch.setattr(deezer, "_get_json", lambda url: {"data": []})
    assert deezer.enrich("Song", "Artist") == deezer._empty()


def test_enrich_none_on_deezer_error_body(monkeypatch):
    # Deezer returns HTTP 200 with an {"error": ...} body for quota/invalid requests.
    monkeypatch.setattr(deezer, "_get_json",
                        lambda url: {"error": {"type": "Exception", "message": "quota"}})
    assert deezer.enrich("Song", "Artist") == deezer._empty()


def test_enrich_playlist_fills_bpm(store, monkeypatch):
    iid = store.upsert_identity("main", "cred", None, True)
    t1 = store.upsert_track("v1", "Song", "Artist", "Alb", 200)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1000.0)
    store.set_playlist_tracks(pid, [t1])
    monkeypatch.setattr(deezer, "enrich",
                        lambda title, artist: {**deezer._empty(), "bpm": 140.0})
    events = []
    deezer.enrich_playlist(store, pid, events.append)
    assert store.get_track_audio(t1)[0] == 140.0
    assert any(e.get("type") == "done" for e in events)
