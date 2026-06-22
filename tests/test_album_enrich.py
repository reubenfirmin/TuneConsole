"""Album enrichment parity: saved albums get enrich candidates/cards, an enrichable album page, and
the same runners (scoped to the album's folded-in tracks)."""
from fastapi.testclient import TestClient

from yt_playlist.providers import musicbrainz
from yt_playlist.rec import recommend
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _fold_album(store, bid="BID", n=4):
    """Fold an n-track, genre-less album into the library + register it as saved."""
    for i in range(n):
        store.upsert_track(f"v{i}", f"T{i}", "Art", "Greatest Hits", None, album_browse_id=bid)
    store.replace_saved_albums([{"browse": bid, "title": "Greatest Hits", "artist": "Art",
                                 "year": "2001", "type": "Album", "thumbnail": None}])


def test_album_enrich_candidates_and_card(store):
    store.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    cands = store.album_enrichment_candidates(min_gaps=3)
    assert cands and cands[0]["browse_id"] == "BID" and cands[0]["gaps"] == 4
    cards = recommend.take_action(store, now=1.0, auth_expired={})
    assert any(c.key == "enrich-album:BID" and c.cta_href == "/album?browse=BID&enrich=1" for c in cards)


def test_album_enrich_runner_fills_genres(store, monkeypatch):
    _fold_album(store)
    pending = store.album_tracks_to_enrich("BID")
    assert len(pending) == 4
    monkeypatch.setattr(musicbrainz, "enrich", lambda title, artist: ("rock", "2001"))

    musicbrainz.enrich_playlist(store, None, lambda ev: None, pending=pending)

    detail = store.album_tracks_detail("BID")
    assert all(t["genre"] == "rock" and t["year"] == "2001" for t in detail)
    assert store.album_tracks_to_enrich("BID") == []          # nothing left to enrich


def test_album_page_renders_enrichable_table(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/album?browse=BID").text                    # FakeClient has no get_album -> store fallback
    assert "Greatest Hits" in html and "Genre" in html        # enrichable view, not "unavailable"
    assert "Enrich via MusicBrainz" in html


def test_album_enrich_endpoint_starts_job(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    r = c.post("/album/BID/enrich/musicbrainz")
    assert r.status_code == 200 and "job_id" in r.json()


def test_album_track_genre_edit(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    r = c.post("/album/BID/track-genre", data={"video_id": "v0", "genre": "jazz"})
    assert r.status_code == 200 and "jazz" in r.text                 # re-rendered row carries it
    assert any(t["genre"] == "jazz" for t in store.album_tracks_detail("BID") if t["video_id"] == "v0")


def test_album_folds_in_on_demand_when_saved(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.replace_saved_albums([{"browse": "BID", "title": "Kind of Blue", "artist": "Miles Davis",
                                 "year": "1959", "type": "Album", "thumbnail": None}])
    album = {"title": "Kind of Blue", "artists": [{"name": "Miles Davis"}], "thumbnails": [{"url": "t"}],
             "tracks": [{"title": "So What", "videoId": "v1", "artists": [{"name": "Miles Davis"}]}]}
    c = TestClient(create_app(store, lambda: {iid: FakeClient(albums={"BID": album})}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")

    assert store.album_tracks_detail("BID") == []        # not folded in yet
    c.get("/album?browse=BID")                            # opening it folds the saved album's tracks in
    assert any(t["video_id"] == "v1" for t in store.album_tracks_detail("BID"))


def test_enrich_cards_capped_at_three(store):
    store.upsert_identity("main", "cred", None, True)
    for n in range(4):                                    # 4 gappy saved albums -> 4 cards before the cap
        for i in range(4):
            store.upsert_track(f"B{n}v{i}", f"T{i}", "Art", f"Alb{n}", None, album_browse_id=f"B{n}")
    store.replace_saved_albums([{"browse": f"B{n}", "title": f"Alb{n}", "artist": "Art",
                                 "year": None, "type": "Album", "thumbnail": None} for n in range(4)])
    cards = recommend.take_action(store, now=1.0, auth_expired={})
    assert sum(1 for c in cards if c.kind == "enrich") == 3
