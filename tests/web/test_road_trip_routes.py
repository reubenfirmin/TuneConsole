import json

from fastapi.testclient import TestClient

from yt_playlist.repos.rec_query import GENERATED_GROUP
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _app(store, client=None):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: client or FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def test_road_trip_page_renders(store):
    c, _ = _app(store)
    r = c.get("/road_trip")
    assert r.status_code == 200
    assert "Road Trip" in r.text


def test_save_list_and_delete_recipe(store):
    c, _ = _app(store)
    r = c.post("/road_trip/recipes", data={
        "name": "Beach Run", "own_pct": "60", "target_minutes": "240",
        "artists": json.dumps(["Tame Impala"]), "genres": json.dumps(["synthpop"]),
        "blacklist_genres": json.dumps(["country"])})
    assert r.status_code == 200
    assert "Beach Run" in r.text
    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    rid = recipes[0]["id"]

    r = c.delete(f"/road_trip/recipes/{rid}")
    assert r.status_code == 200
    assert store.list_road_trip_recipes() == []


def test_generate_creates_generated_playlist_and_updates_last_playlist(store, monkeypatch):
    from yt_playlist.web.routes import road_trip as road_trip_route

    fake_tracks = [{"video_id": "v1", "title": "Song A", "artist": "Artist A", "album": "",
                    "thumbnail": None, "duration": 200, "genre": "", "source": "mine"}]
    monkeypatch.setattr(road_trip_route.road_trip_rec, "assemble_playlist",
                        lambda store, client, recipe, now: (
                            fake_tracks, {"achieved_minutes": 3.3, "own_count": 1, "their_count": 0}))

    c, iid = _app(store)
    rid = store.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], [], [], 5, 1000.0)

    r = c.post(f"/road_trip/recipes/{rid}/generate")
    assert r.status_code == 200

    recipe = store.get_road_trip_recipe(rid)
    assert recipe["last_playlist_id"] is not None
    groups = store.get_playlist_groups()
    assert groups[recipe["last_playlist_id"]] == GENERATED_GROUP


def test_generate_missing_recipe_404s(store):
    c, _ = _app(store)
    r = c.post("/road_trip/recipes/999/generate")
    assert r.status_code == 404


def test_autocomplete_artists(store):
    client = FakeClient(search_results=[
        {"artist": "Tame Impala", "browseId": "UC1"}, {"artist": "Tame Impala", "browseId": "UC1"}])
    c, _ = _app(store, client)
    r = c.get("/road_trip/autocomplete/artists?q=Tame")
    assert r.status_code == 200
    assert r.json()["results"] == ["Tame Impala"]     # deduped


def test_autocomplete_artists_empty_query(store):
    c, _ = _app(store)
    assert c.get("/road_trip/autocomplete/artists?q=").json()["results"] == []


def test_road_trip_page_shows_saved_recipe_and_last_playlist_link(store):
    store.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], ["synthpop"], ["country"],
                                240, 1000.0)
    rid = store.list_road_trip_recipes()[0]["id"]
    store.set_road_trip_last_playlist(rid, "PL_ABC")

    c, _ = _app(store)
    html = c.get("/road_trip").text

    assert "Beach Run" in html
    assert "Tame Impala" in html
    assert "synthpop" in html
    assert "country" in html
    assert "60" in html                 # own_pct shown somewhere
    assert "PL_ABC" in html             # last-generated playlist link present


def test_road_trip_nav_link_present(store):
    c, _ = _app(store)
    html = c.get("/road_trip").text
    assert 'href="/road_trip"' in html
