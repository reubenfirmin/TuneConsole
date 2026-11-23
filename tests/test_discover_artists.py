import pytest

from tests import model_support
from tests.conftest import FakeClient
from yt_playlist.providers import lastfm
from yt_playlist.rec import discover
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
    iid = store.accounts.upsert_account("main", None, True)
    band = [store.tracks.upsert_track(f"c{i}", f"C{i}", "CoreBand", None, None) for i in range(10)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PC", "Core Mix", 10, "h", 0.0), band)
    for _ in range(5):
        store.history.record_history_plays(iid, 1.0, ["c0|coreband"], source_timezone="UTC")
    model_support.build_track_model(store, dim=4)
    store.settings.set_setting("lastfm_api_key", "KEY")

    calls = []
    def fake(name, key, limit=50):
        calls.append(name)
        return [("Fresh Artist", 0.9), ("CoreBand", 0.5)]       # 'CoreBand' is owned -> excluded
    monkeypatch.setattr(lastfm, "similar_artists", fake)
    monkeypatch.setattr(lastfm, 'artist_genre', lambda *args, **kwargs: 'House')

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
    store.accounts.upsert_account("main", None, True)
    assert discover.new_artists(_ctx(store)) == []


def test_new_artists_uses_a_starter_anchor_without_lastfm_or_vectors(store, monkeypatch):
    monkeypatch.setattr(lastfm, 'api_key', lambda store: None)
    store.discovery.select_starter_artist('Seed', 'UCseed0000', '', True, 1000)
    ctx = _ctx(store)
    page = {'related': {'results': [{'title': 'Peer', 'browseId': 'UCpeer0000',
                                     'thumbnails': [{'url': 'https://example.com/peer.png'}]}]}}
    ctx.client_provider = lambda: {1: FakeClient(artists={'UCseed0000': page})}

    result = discover.new_artists(ctx)
    assert len(result) == 1
    assert (result[0]['artist'], result[0]['browse_id'], result[0]['because'], result[0]['fits']) == (
        'Peer', 'UCpeer0000', ['Seed'], [])
    assert list(result[0]['sources'].values()) == [{'youtube': 1.}]


@pytest.mark.parametrize('cached', [False, True], ids=['fresh', 'legacy-cache'])
def test_discover_homonyms_keep_only_attributable_provider_evidence(store, monkeypatch, cached):
    seeds = [('Same Name', 'UCseed0000', 'First peer'), ('Same Name', 'UCseed1111', 'Second peer'),
             ('Other Name', 'UCseed2222', 'Third peer')]
    for name, channel, peer in seeds:
        store.discovery.select_starter_artist(name, channel, '', True, 1000)
        if cached and name == 'Same Name':
            identity = store.artists.provider_identity(channel)
            store.artist_similarity.cache_similar(identity, [('Ambiguous match', .99)], 990)
    ctx = _ctx(store)
    pages = {channel: {'related': {'results': [{'title': peer, 'browseId': 'UCpeer' + channel[-4:]}]}}
             for _, channel, peer in seeds}
    ctx.client_provider = lambda: {1: FakeClient(artists=pages)}
    store.settings.set_setting('lastfm_api_key', 'KEY')
    calls = []

    def similar(name, key):
        calls.append(name)
        return [('Identified match', .8)] if name == 'Other Name' else [('Ambiguous match', .99)]

    monkeypatch.setattr(lastfm, 'similar_artists', similar)
    monkeypatch.setattr(lastfm, 'artist_genre', lambda *args, **kwargs: None)
    result = {row['artist']: row['sources'] for row in discover.new_artists(ctx)}
    assert calls == ['Other Name']
    assert 'Ambiguous match' not in result
    for _, channel, peer in seeds:
        assert result[peer] == {store.artists.key(store.artists.provider_identity(channel)): {'youtube': 1.}}
    assert list(result['Identified match'].values()) == [{'lastfm': .8}]
