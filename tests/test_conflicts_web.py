"""The conflict header icon + resolver modal + resolution overwrite (playlist scope)."""
import html
import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _seed(store, iid):
    a = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.tracks.upsert_track("v0", "Hyperballad", "Bjork", "Post", 200, 1)
    store.playlists.set_playlist_tracks(a, [tid])
    store.tracks.set_track_enrichment(tid, "Electronic", "1995")     # canonical = MusicBrainz's pick
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                         {"provider": "discogs", "value": "Art Pop"}])
    return a, tid


def test_page_passes_conflict_count_to_panel(store):
    c, iid = _client(store)
    a, _tid = _seed(store, iid)
    html = c.get(f"/playlist/{a}").text
    assert f"enrichPanel({a}, false, 0, '', null, 1)" in html   # conflict_count = 1 lights the icon


def test_resolver_lists_candidates(store):
    c, iid = _client(store)
    a, _tid = _seed(store, iid)
    html = c.get(f"/playlist/{a}/conflicts").text
    assert "Hyperballad" in html
    assert "Electronic" in html and "Art Pop" in html
    assert "musicbrainz" in html and "discogs" in html


def test_resolve_overwrites_column_and_clears_conflict(store):
    c, iid = _client(store)
    a, tid = _seed(store, iid)
    r = c.post(f"/playlist/{a}/conflicts/resolve", data={f"{tid}:genre": "Art Pop"})
    assert r.status_code == 200
    # canonical column overwritten with the user's pick...
    assert store.conn.execute("SELECT genre FROM tracks WHERE id=?", (tid,)).fetchone()["genre"] == "Art Pop"
    # ...and the conflict no longer counts
    assert store.enrichment.conflict_count_for_playlist(a) == 0
    assert "Nothing left to resolve" in r.text


@pytest.mark.parametrize("scope", ["playlist", "album"])
def test_provider_links_look_up_track_and_survive_partial_resolution(store, scope):
    c, iid = _client(store)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    title, artist = 'Critical Choice / "Live"', "Björk & Friends"
    tid = store.tracks.upsert_track("v0", title, artist, "Album", 200, 1, album_browse_id="BID")
    store.playlists.set_playlist_tracks(pid, [tid])
    mbid = "b1234567-1234-1234-1234-123456789abc"
    store.tracks.set_track_mbid(tid, mbid)
    providers = ["musicbrainz", "discogs", "lastfm", "deezer", "acousticbrainz"]
    store.enrichment.upsert_conflict(tid, "year", [{"provider": p, "value": str(1952 + i)}
                                         for i, p in enumerate(providers)])
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                          {"provider": "discogs", "value": "Art Pop"}])
    base = f"/playlist/{pid}" if scope == "playlist" else "/album/BID"

    def check_links(body):
        links = {provider: urlsplit(html.unescape(url)) for url, provider in re.findall(
            r'<a class="conflict-prov" href="([^"]+)"[^>]*>([^<]+)</a>', body)}
        assert set(links) == set(providers)
        query = f"{artist} {title}"
        assert links["musicbrainz"].netloc == "musicbrainz.org"
        assert parse_qs(links["musicbrainz"].query)["query"] == [query]
        for provider, host in [("discogs", "www.discogs.com"), ("lastfm", "www.last.fm")]:
            assert links[provider].netloc == host
            assert parse_qs(links[provider].query)["q"] == [query]
        assert links["deezer"].netloc == "www.deezer.com"
        assert unquote(links["deezer"].path) == f"/search/{query}/track"
        assert links["acousticbrainz"].geturl() == f"https://acousticbrainz.org/{mbid}"
        assert body.count('target="_blank" rel="noopener noreferrer"') == 7

    check_links(c.get(f"{base}/conflicts").text)
    response = c.post(f"{base}/conflicts/resolve", data={f"{tid}:genre": "Art Pop"})
    assert response.status_code == 200
    # The remaining year disagreement still offers provider links after the modal swaps.
    body = response.text
    assert "1952" in body and "Art Pop" not in body
    assert body.count('target="_blank" rel="noopener noreferrer"') == 5


def test_unknown_provider_remains_plain_text(store):
    c, iid = _client(store)
    pid, tid = _seed(store, iid)
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "unknown", "value": "Rock"},
                                          {"provider": "discogs", "value": "Art Pop"}])
    body = c.get(f"/playlist/{pid}/conflicts").text
    assert '<span class="muted conflict-prov">unknown</span>' in body
    assert "https://www.discogs.com/search/" in body
