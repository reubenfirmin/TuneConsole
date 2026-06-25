# tests/test_acousticbrainz.py
from yt_playlist.providers import acousticbrainz as ab


HIGHLEVEL = {"highlevel": {
    "danceability": {"all": {"danceable": 0.92, "not_danceable": 0.08}},
    "mood_party": {"all": {"party": 0.87, "not_party": 0.13}},
    "mood_aggressive": {"all": {"aggressive": 0.30, "not_aggressive": 0.70}},
    "mood_happy": {"all": {"happy": 0.70, "not_happy": 0.30}},
    "mood_sad": {"all": {"sad": 0.20, "not_sad": 0.80}},
    "mood_relaxed": {"all": {"relaxed": 0.60, "not_relaxed": 0.40}},
    "mood_acoustic": {"all": {"acoustic": 0.30, "not_acoustic": 0.70}},
    "voice_instrumental": {"all": {"instrumental": 0.10, "voice": 0.90}},
}}
LOWLEVEL = {"rhythm": {"bpm": 128.0}, "tonal": {"key_key": "A", "key_scale": "minor"},
            "lowlevel": {"average_loudness": 0.9, "dynamic_complexity": 3.2}}


def test_derive_energy_blends_mood_models():
    # 0.5*0.87 + 0.3*0.30 + 0.2*0.92 = 0.435 + 0.09 + 0.184 = 0.709
    assert ab.derive_energy(HIGHLEVEL["highlevel"]) == 0.709


def test_enrich_returns_feature_dict(monkeypatch):
    def fake_get_json(url):
        if url.endswith("/high-level"):
            return HIGHLEVEL
        if url.endswith("/low-level"):
            return LOWLEVEL
        raise AssertionError(url)
    monkeypatch.setattr(ab, "_get_json", fake_get_json)
    feat = ab.enrich("mbid-1")
    assert feat["bpm"] == 128.0
    assert feat["energy"] == 0.709
    assert feat["danceability"] == 0.92
    assert feat["music_key"] == "A"
    assert feat["music_scale"] == "minor"
    assert feat["mood_happy"] == 0.70
    assert feat["mood_sad"] == 0.20
    assert feat["mood_relaxed"] == 0.60
    assert feat["mood_acoustic"] == 0.30
    assert feat["instrumental"] == 0.10
    assert feat["loudness"] == 0.9
    assert feat["dynamic_complexity"] == 3.2


def test_enrich_none_when_no_data(monkeypatch):
    import urllib.error

    def fake_get_json(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(ab, "_get_json", fake_get_json)
    assert ab.enrich("mbid-unknown") == ab._empty()
    assert not ab._breaker.tripped()   # a 404 is "reachable", must not trip the breaker


def test_enrich_playlist_resolves_mbid_then_fills(store, monkeypatch):
    iid = store.upsert_identity("main", "cred", None, True)
    t1 = store.upsert_track("v1", "Song", "Artist", "Alb", 200)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1000.0)
    store.set_playlist_tracks(pid, [t1])
    # track has no stored MBID -> provider resolves it live, persists it, then enriches
    monkeypatch.setattr(ab, "enrich",
                        lambda mbid: {**ab._empty(), "bpm": 130.0, "energy": 0.6,
                                      "danceability": 0.8})
    events = []
    ab.enrich_playlist(store, pid, events.append, mbid_fn=lambda title, artist: "mbid-live")
    assert store.get_track_audio(t1) == (130.0, 0.6, 0.8)
    rows = store.tracks_missing_audio(pid)
    assert rows == []  # fully populated now
    assert any(e.get("type") == "done" for e in events)
