"""Album enrichment: saved albums get an enrichable album page and the shared waterfall (scoped to the
album's folded-in tracks)."""
import threading

from fastapi.testclient import TestClient

from yt_playlist.providers import musicbrainz, waterfall
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _fold_album(store, bid="BID", n=4):
    """Fold an n-track, genre-less album into the library + register it as saved."""
    for i in range(n):
        store.tracks.upsert_track(f"v{i}", f"T{i}", "Art", "Greatest Hits", None, album_browse_id=bid)
    store.collection.replace_saved_albums([{"browse": bid, "title": "Greatest Hits", "artist": "Art",
                                 "year": "2001", "type": "Album", "thumbnail": None}])


def test_album_waterfall_fills_genres(store, monkeypatch):
    _fold_album(store)
    pending = store.tracks.album_tracks_to_enrich("BID")
    assert len(pending) == 4
    monkeypatch.setattr(musicbrainz, "enrich_full", lambda title, artist: ("rock", "2001", None, [], []))

    waterfall.run_waterfall(store, pending, [{"name": "musicbrainz", "enabled": True}],
                            lambda ev: None)

    detail = store.charts.album_tracks_detail("BID")
    assert all(t["genre"] == "rock" and t["year"] == "2001" for t in detail)
    assert store.tracks.album_tracks_to_enrich("BID") == []          # nothing left to enrich


def test_album_page_renders_enrichable_table(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/album?browse=BID").text                    # FakeClient has no get_album -> store fallback
    assert "Greatest Hits" in html and "Genre" in html        # enrichable view, not "unavailable"
    assert 'aria-label="Enrich"' in html                       # the single waterfall enrich icon


def test_album_enrich_endpoint_starts_job(store, monkeypatch):
    # Exercise job dispatch without leaving live provider requests running into later tests.
    started = threading.Event()
    pending_ids = []

    def enrich(store, pending, config, on_progress):
        pending_ids.extend(t["id"] for t in pending)
        started.set()

    monkeypatch.setattr(waterfall, "run_waterfall", enrich)
    iid = store.identities.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    r = c.post("/album/BID/enrich")
    assert r.status_code == 200 and "job_id" in r.json()
    assert started.wait(2)
    assert set(pending_ids) == {t["id"] for t in store.tracks.album_tracks_for_waterfall("BID")}


def test_album_track_genre_edit(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    _fold_album(store)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    r = c.post("/album/BID/track-genre", data={"video_id": "v0", "genre": "jazz"})
    assert r.status_code == 200 and "jazz" in r.text                 # re-rendered row carries it
    assert any(t["genre"] == "jazz" for t in store.charts.album_tracks_detail("BID") if t["video_id"] == "v0")


def test_album_folds_in_on_demand_when_saved(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.collection.replace_saved_albums([{"browse": "BID", "title": "Kind of Blue", "artist": "Miles Davis",
                                 "year": "1959", "type": "Album", "thumbnail": None}])
    album = {"title": "Kind of Blue", "artists": [{"name": "Miles Davis"}], "thumbnails": [{"url": "t"}],
             "tracks": [{"title": "So What", "videoId": "v1", "artists": [{"name": "Miles Davis"}]}]}
    c = TestClient(create_app(store, lambda: {iid: FakeClient(albums={"BID": album})}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")

    assert store.charts.album_tracks_detail("BID") == []        # not folded in yet
    c.get("/album?browse=BID")                            # opening it folds the saved album's tracks in
    assert any(t["video_id"] == "v1" for t in store.charts.album_tracks_detail("BID"))
