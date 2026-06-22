from yt_playlist import discover, embed, lastfm
from yt_playlist.web.context import Ctx


def test_similar_artists_parses_matches(monkeypatch):
    monkeypatch.setattr(lastfm, "_get", lambda params: {"similarartists": {"artist": [
        {"name": "Rrose", "match": "0.95"}, {"name": "Donato Dozzy", "match": "0.80"}]}})
    assert lastfm.similar_artists("Recondite", "KEY") == [("Rrose", 0.95), ("Donato Dozzy", 0.80)]


def test_similar_artists_empty_on_error(monkeypatch):
    def boom(params):
        raise OSError("network")
    monkeypatch.setattr(lastfm, "_get", boom)
    assert lastfm.similar_artists("X", "KEY") == []


def _ctx(store):
    return Ctx(store=store, client_provider=lambda: {}, now_fn=lambda: 1000.0, templates=None, jobs=None)


def test_new_artists_taste_pinned_and_cached(store, monkeypatch):
    iid = store.upsert_identity("main", "cred", None, True)
    band = [store.upsert_track(f"c{i}", f"C{i}", "CoreBand", None, None) for i in range(10)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PC", "Core Mix", 10, "h", 0.0), band)
    for _ in range(5):
        store.add_history_snapshot(iid, 1.0, ["c0|coreband"])   # play -> anchor + playlist weight
    embed.build_and_store(store, dim=4)
    store.set_setting("lastfm_api_key", "KEY")

    calls = []
    def fake(name, key, limit=50):
        calls.append(name)
        return [("Fresh Artist", 0.9), ("CoreBand", 0.5)]       # 'CoreBand' is owned -> excluded
    monkeypatch.setattr(lastfm, "similar_artists", fake)

    out = discover.new_artists(_ctx(store), limit=5)
    arts = [c["artist"] for c in out]
    assert "Fresh Artist" in arts
    assert "CoreBand" not in arts
    fa = next(c for c in out if c["artist"] == "Fresh Artist")
    assert "CoreBand" in fa["because"]            # bridged via your anchor artist
    assert "Core Mix" in fa["fits"]               # fits the playlist you actually play
    n = len(calls)
    discover.new_artists(_ctx(store), limit=5)     # second run hits the cache
    assert len(calls) == n


def test_new_artists_empty_without_key(store):
    store.upsert_identity("main", "cred", None, True)
    assert discover.new_artists(_ctx(store)) == []
