import json
import threading
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from yt_playlist.rec import road_trip as road_trip_rec
from yt_playlist.web.routes import road_trip as road_trip_route
from yt_playlist.repos.rec_query import GENERATED_GROUP
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _app(store, client=None, monkeypatch=None):
    iid = store.upsert_identity("main", "cred", None, True)
    if monkeypatch is not None:
        # Run the "fill in their half" worker inline, so a build is complete when the POST returns.
        # Its own staging is covered by test_build_returns_your_half_immediately below.
        monkeypatch.setattr(road_trip_route, "_spawn", lambda fn: fn())
    app = create_app(store, lambda: {iid: client or FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _stub_pools(monkeypatch, store=None, own_count=20, other_count=20):
    """A library of 5-minute songs on your side and a matching pool on theirs, so no route test
    touches the network. Your side is stubbed at the library query, theirs at the per-input fetch."""
    songs = [{"key": f"k{i}", "video_id": f"m{i}", "title": f"Mine {i}",
              "artist": f"Mine Artist {i}", "album": "", "thumbnail": None, "duration": 300,
              "genre": "Rock", "year": 1990, "liked": False} for i in range(own_count)]
    if store is not None:
        monkeypatch.setattr(store, "library_songs", lambda: songs)
        monkeypatch.setattr(store, "play_counts", lambda: {f"k{i}": i for i in range(own_count)})

    def songs_fn(client, kind, name, cap, store=None, decade=None):
        rows = [{"video_id": f"t{i}", "title": f"Theirs {i}", "artist": f"Their Artist {i}",
                 "album": "", "thumbnail": None, "duration": 300, "genre": "synthpop"}
                for i in range(other_count)]
        return rows[:cap], []

    monkeypatch.setattr(road_trip_rec, "other_input_songs", songs_fn)
    monkeypatch.setattr(road_trip_rec, "_facts",
                        lambda title, artist: {"popularity": 900 - int(title.split()[-1]),
                                               "year": 1980, "genre": "synthpop", "duration": 300})


def _recipe(store, **kw):
    store.save_road_trip_recipe(None, kw.get("name", "Beach Run"), kw.get("own_pct", 50),
                                kw.get("artists", ["Tame Impala"]), kw.get("genres", []),
                                kw.get("target_minutes", 30),
                                1000.0, familiarity_pct=kw.get("familiarity_pct", 50))
    return store.list_road_trip_recipes()[0]["id"]


def test_road_trip_page_renders(store):
    c, _ = _app(store)
    r = c.get("/road_trip")
    assert r.status_code == 200
    assert "Road Trip" in r.text


def test_reopening_and_saving_repairs_passenger_genre_membership(store):
    seeds = ["Weezer", "We Were Promised Jetpacks", "Staind"]
    client = FakeClient()
    c, _ = _app(store, client)
    rid = _recipe(store, own_pct=0, artists=seeds, target_minutes=15)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=7)
    state.update(building=False, pending=[], artist_contexts={})
    for name, genre in zip(seeds, ["Alternative Rock", "Indie Rock", "Alternative Rock"]):
        state["artist_contexts"][name] = {"genre": genre, "year": None, "related": []}
        candidate = road_trip_rec._candidate(name, name, name, "", None, 300, "theirs", genre, None)
        candidate.update(input=name, cluster_genre="Alternative Rock", anchors=seeds, score=1)
        state["pool"].append(candidate)
    stale = road_trip_rec._candidate("old", "Old singer song", "Old Singer", "", None, 300,
                                     "theirs", "Singer-Songwriter", None)
    stale["input"] = "Removed Artist"
    state["pool"].append(stale)
    state["picked"] = [{k: row[k] for k in road_trip_rec._ROW_FIELDS} for row in state["pool"]]
    state["picks"] = [row["video_id"] for row in state["picked"]]
    state["axes"]["theirs"] = [{"key": "genre:" + genre, "kind": "genre", "name": genre,
                                "share": .33, "target": None, "artists": seeds}
                               for genre in ("Alternative Rock", "Indie Rock", "Singer-Songwriter")]
    store.save_road_trip_draft(rid, state, 1000)

    html = c.get("/road_trip").text

    assert "Like Weezer, Staind" in html
    assert "Like We Were Promised Jetpacks</small>" in html
    assert "Like Weezer, We Were Promised Jetpacks" not in html
    assert "Passenger genre balance" in html
    assert "Old singer song" not in html
    assert c.post(f"/road_trip/draft/{rid}/save").status_code == 200
    saved = store.get_road_trip_draft(rid)
    assert set(saved["picks"]) == set(seeds)
    assert client.added == [(saved["saved_playlist_id"], saved["picks"])]


def test_new_route_is_a_library_action_and_hides_the_active_draft(store):
    c, _ = _app(store)
    rid = _recipe(store)
    store.save_road_trip_draft(rid, {"name": "Old draft", "stats": {}, "picked": []}, 1000.0)

    html = c.post("/road_trip/new").text

    assert "Build a route" not in html       # the mixer is the creation surface, not a CRUD heading
    assert "Old draft" not in html
    assert 'placeholder="Beach Run"' in html


def test_save_list_and_delete_recipe(store):
    c, _ = _app(store)
    r = c.post("/road_trip/recipes", data={
        "name": "Beach Run", "own_pct": "60", "familiarity_pct": "80", "target_minutes": "240",
        "artists": json.dumps(["Tame Impala"]), "genres": json.dumps(["synthpop"]),
        "blacklist_genres": json.dumps(["Metal"])})
    assert r.status_code == 200
    assert "Beach Run" in r.text
    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    assert recipes[0]["own_pct"] == 60 and recipes[0]["familiarity_pct"] == 80
    assert recipes[0]["blacklist_genres"] == ["Metal"]
    rid = recipes[0]["id"]

    r = c.delete(f"/road_trip/recipes/{rid}")
    assert r.status_code == 200
    assert store.list_road_trip_recipes() == []


def test_build_makes_an_on_screen_draft_and_touches_no_playlist(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    client = FakeClient()
    c, _ = _app(store, client, monkeypatch)
    rid = _recipe(store)

    r = c.post(f"/road_trip/recipes/{rid}/build")

    assert r.status_code == 200
    assert "Save to YouTube" in r.text                 # the playlist is on the page, not on YouTube
    assert client.created == []
    state = store.get_road_trip_draft(rid)
    assert state["stats"]["own_count"] == state["stats"]["their_count"] == 3   # 30 min, 50/50
    assert "Mine 0" in r.text or any("Mine" in t["title"] for t in road_trip_rec.draft_tracks(state))


def test_a_thin_blend_pool_explains_the_missing_song_share(store, monkeypatch):
    _stub_pools(monkeypatch, store, own_count=2)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store, genres=["Rock"], target_minutes=60)

    response = c.post(f"/road_trip/recipes/{rid}/build")

    assert response.status_code == 200
    assert "Blend has 2 of the 4 songs needed for its requested share" in " ".join(response.text.split())
    state = store.get_road_trip_draft(rid)
    assert state["stats"]["blend_count"] == 2
    assert len(state["picked"]) == 12


def test_build_returns_your_half_immediately_then_fills_in_theirs(store, monkeypatch):
    """Pressing Build must not block on YouTube: the response already carries a playlist of your
    tracks, and their half arrives afterwards, one input at a time."""
    _stub_pools(monkeypatch, store)
    workers = []
    monkeypatch.setattr(road_trip_route, "_spawn", workers.append)     # hold the worker back
    c, _ = _app(store)
    rid = _recipe(store, artists=["Tame Impala", "MGMT"])

    r = c.post(f"/road_trip/recipes/{rid}/build")

    assert "Mine 0" in r.text or "rt-row" in r.text
    mid_build = store.get_road_trip_draft(rid)
    assert mid_build["building"] is True
    assert mid_build["stats"]["own_count"] > 0          # your side is already on screen
    assert mid_build["stats"]["their_count"] == 0        # theirs hasn't been fetched yet
    assert [p["name"] for p in mid_build["pending"]] == ["Tame Impala", "MGMT"]
    assert "Adding their tracks" in r.text               # and the page says so, and polls
    assert "/progress" in r.text

    workers[0]()                                         # let the background worker run

    done = store.get_road_trip_draft(rid)
    assert done["building"] is False and done["pending"] == []
    assert done["stats"]["their_count"] > 0
    assert "Save to YouTube" in c.get(f"/road_trip/draft/{rid}/progress").text


def test_rebuilding_gives_a_different_playlist(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)

    runs = []
    for _ in range(4):
        c.post(f"/road_trip/recipes/{rid}/build")
        runs.append(tuple(store.get_road_trip_draft(rid)["picks"]))

    assert len(set(runs)) == 4


def test_build_without_an_account_explains_itself(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    rid = _recipe(store)

    r = c.post(f"/road_trip/recipes/{rid}/build")

    assert "Connect an account" in r.text
    assert store.get_road_trip_draft(rid) is None


def test_build_missing_recipe_404s(store):
    c, _ = _app(store)
    assert c.post("/road_trip/recipes/999/build").status_code == 404


def test_mix_slider_repicks_without_rebuilding(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")

    r = c.post(f"/road_trip/draft/{rid}/mix", data={"own_pct": "100"})

    assert r.status_code == 200
    state = store.get_road_trip_draft(rid)
    assert state["own_pct"] == 100
    assert state["stats"]["their_count"] == 0
    assert store.get_road_trip_recipe(rid)["own_pct"] == 50    # the saved recipe is left alone


def test_mix_slider_drags_toward_mine_on_the_left(store, monkeypatch):
    """The bar's left label is "Mine", so it carries THEIR share: 0 = all mine."""
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")

    c.post(f"/road_trip/draft/{rid}/mix", data={"their_pct": "0"})

    state = store.get_road_trip_draft(rid)
    assert state["own_pct"] == 100
    assert state["stats"]["their_count"] == 0

    c.post(f"/road_trip/draft/{rid}/mix", data={"their_pct": "100"})
    assert store.get_road_trip_draft(rid)["stats"]["own_count"] == 0


def test_editing_the_recipe_steers_the_draft_in_place(store, monkeypatch):
    """The form is a control panel, not a create dialog: editing the recipe a draft is showing
    re-steers that draft rather than starting a new one."""
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store, artists=["Tame Impala"], target_minutes=30)
    c.post(f"/road_trip/recipes/{rid}/build")
    before = store.get_road_trip_draft(rid)
    assert before["stats"]["minutes"] <= 35

    r = c.post("/road_trip/recipes", data={
        "id": str(rid), "name": "Beach Run", "own_pct": "50", "familiarity_pct": "50",
        "target_minutes": "60", "artists": json.dumps(["Tame Impala"]), "genres": json.dumps([])})

    assert r.status_code == 200
    after = store.get_road_trip_draft(rid)
    assert after["target_minutes"] == 60                 # the same draft, re-steered
    assert after["stats"]["minutes"] > before["stats"]["minutes"]
    assert after["seed"] == before["seed"]                # not a rebuild
    assert len(store.list_road_trip_recipes()) == 1        # and not a second recipe


def test_added_genre_is_balanced_before_discovery_and_can_grow_a_full_pool(store, monkeypatch):
    c, _ = _app(store)
    genres = ["Rock", "Pop", "Jazz"]
    rid = _recipe(store, own_pct=0, artists=[], genres=genres, target_minutes=100)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=7)
    state["pool"] = [road_trip_rec._candidate(
        f"old-{i}", f"Old {i}", f"Old Artist {i}", "", None, 300, "theirs", genres[i % 3], 1995)
        for i in range(state["other_limit"])]
    for row in state["pool"]:
        row.update(score=1, input=row["genre"])
    state["targets"]["theirs"] = {"genre:" + g: p for g, p in zip(genres, [.6, .3, .1])}
    state["done_inputs"] = genres.copy()
    road_trip_rec.finish_draft(state, store, 1000)
    store.save_road_trip_draft(rid, state, 1000)
    held = []
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)
    monkeypatch.setattr(road_trip_rec, "artist_genre", lambda *args: None)
    monkeypatch.setattr(road_trip_rec, "_facts", lambda *args: {"popularity": 500, "year": 1995})
    monkeypatch.setattr(road_trip_rec, "other_input_songs", lambda *args: ([
        {"video_id": f"funk-{i}", "title": f"Funk {i}", "artist": f"Funk Artist {i}", "album": "",
         "thumbnail": None, "duration": 300, "genre": "Funk"} for i in range(8)], []))
    data = {"id": str(rid), "name": "Beach Run", "own_pct": "0", "target_minutes": "100",
            "artists": "[]", "genres": json.dumps(genres + ["Funk"])}
    try:
        html = c.post("/road_trip/recipes", data=data).text
        waiting = store.get_road_trip_draft(rid)
        expected = {"genre:Rock": .45, "genre:Pop": .23, "genre:Jazz": .07, "genre:Funk": .25}
        assert waiting["targets"]["theirs"] == expected
        assert waiting["building"] and held
        assert not any(row["genre"] == "Funk" for row in waiting["pool"])
        assert waiting["other_limit"] > len(waiting["pool"])
        assert 'aria-label="Funk"' in html
        assert 'aria-busy="true"' in html
    finally:
        for worker in held:
            worker()
    done = store.get_road_trip_draft(rid)
    assert done["targets"]["theirs"] == expected
    assert any(row["genre"] == "Funk" for row in done["picked"])
    data["genres"] = json.dumps(genres)
    c.post("/road_trip/recipes", data=data)
    removed = store.get_road_trip_draft(rid)
    assert "genre:Funk" not in removed["targets"]["theirs"]
    assert not any(row["genre"] == "Funk" for row in removed["pool"])


def test_adding_an_artist_automatically_adds_and_balances_its_genre(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    held = []
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)
    monkeypatch.setattr(road_trip_rec, "artist_genre",
                        lambda store, name: "Alternative Rock" if name == "Weezer" else None)
    data = {"id": str(rid), "name": "Beach Run", "own_pct": "50", "target_minutes": "30",
            "artists": json.dumps(["Tame Impala", "Weezer"]), "genres": "[]"}
    try:
        html = c.post("/road_trip/recipes", data=data).text
        assert store.get_road_trip_recipe(rid)["genres"] == ["Alternative Rock"]
        waiting = store.get_road_trip_draft(rid)
        assert waiting["targets"]["theirs"]["genre:Alternative Rock"] == .5
        assert 'aria-label="Alternative Rock"' in html
        # A deliberate removal on a later edit must not be undone by the existing artist.
        c.post("/road_trip/recipes", data=data)
        assert store.get_road_trip_recipe(rid)["genres"] == []
    finally:
        for worker in held:
            worker()


def test_removing_an_artist_takes_their_tracks_out_of_the_draft(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store, artists=["Tame Impala"])
    c.post(f"/road_trip/recipes/{rid}/build")
    assert store.get_road_trip_draft(rid)["stats"]["their_count"] > 0

    c.post("/road_trip/recipes", data={
        "id": str(rid), "name": "Beach Run", "own_pct": "50", "familiarity_pct": "50",
        "target_minutes": "30", "artists": json.dumps([]), "genres": json.dumps([])})

    state = store.get_road_trip_draft(rid)
    assert [c_ for c_ in state["pool"] if c_["source"] == "theirs"] == []
    assert state["stats"]["their_count"] == 0


def test_edit_loads_a_recipe_into_the_form(store):
    c, _ = _app(store)
    rid = _recipe(store, name="Ski Trip")
    r = c.post(f"/road_trip/recipes/{rid}/edit")
    assert r.status_code == 200
    assert "Ski Trip" in r.text
    assert "Editing" in r.text or "roadTripForm" in r.text


def test_tilting_a_genre_to_zero_drops_it_from_the_playlist(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    # Sliders are labelled by genre FAMILY (synthpop lives under one), so read the axis off the draft
    # rather than assuming the sub-genre name.
    axis = next(a["key"] for a in store.get_road_trip_draft(rid)["axes"]["theirs"]
                if a["kind"] == "genre")

    r = c.post(f"/road_trip/draft/{rid}/tilt", data={"party": "theirs", "axis": axis, "share": "0"})

    assert r.status_code == 200
    state = store.get_road_trip_draft(rid)
    assert state["stats"]["their_count"] == 0
    assert axis in {a["key"] for a in state["axes"]["theirs"]}   # slider stays put

    c.post(f"/road_trip/draft/{rid}/unpin", data={"party": "theirs", "axis": axis})
    assert store.get_road_trip_draft(rid)["stats"]["their_count"] > 0


def test_a_slider_past_the_pool_kicks_off_a_search(store, monkeypatch):
    """Dragging a genre past what was drawn queues YouTube searches and keeps the panel polling,
    rather than settling for the pool it happens to have."""
    _stub_pools(monkeypatch, store, other_count=1)     # one 5-min track against a 15-min half
    held = []
    c, _ = _app(store)
    rid = _recipe(store)
    monkeypatch.setattr(road_trip_route, "_spawn", lambda fn: fn())     # build inline
    c.post(f"/road_trip/recipes/{rid}/build")
    axis = next(a["key"] for a in store.get_road_trip_draft(rid)["axes"]["theirs"]
                if a["kind"] == "genre")
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)          # hold the widening worker

    r = c.post(f"/road_trip/draft/{rid}/tilt", data={"party": "theirs", "axis": axis, "share": "100"})

    assert r.status_code == 200
    state = store.get_road_trip_draft(rid)
    assert state["pending"] and all(p["want"] == axis for p in state["pending"])
    assert held, "the search should have been handed to the background worker"
    assert "Adding their tracks" in r.text
    assert "Passenger genre balance" in r.text
    assert 'x-data=\'roadTripBalance(' in r.text
    assert 'aria-busy="true"' in r.text
    assert "Updating the mix…" in r.text


def test_crossing_out_a_slot_refills_it(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    before = list(store.get_road_trip_draft(rid)["picks"])

    r = c.post(f"/road_trip/draft/{rid}/slot/1")

    assert r.status_code == 200
    after = store.get_road_trip_draft(rid)["picks"]
    assert len(after) == len(before)
    assert after[1] != before[1] and after[0] == before[0]
    assert before[1] in store.get_road_trip_draft(rid)["banned"]


def test_shuffle_redraws_from_the_same_pool(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    before = store.get_road_trip_draft(rid)

    c.post(f"/road_trip/draft/{rid}/shuffle")

    after = store.get_road_trip_draft(rid)
    assert after["picks"] != before["picks"]
    assert [c["video_id"] for c in after["pool"]] == [c["video_id"] for c in before["pool"]]


def test_saving_the_draft_creates_the_generated_playlist(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    client = FakeClient()
    c, _ = _app(store, client, monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    picks = store.get_road_trip_draft(rid)["picks"]

    r = c.post(f"/road_trip/draft/{rid}/save")

    assert r.status_code == 200
    recipe = store.get_road_trip_recipe(rid)
    assert recipe["last_playlist_id"] is not None
    assert store.get_playlist_groups()[recipe["last_playlist_id"]] == GENERATED_GROUP
    # what was on screen is exactly what was sent, in order
    assert client.added == [(recipe["last_playlist_id"], picks)]
    assert store.get_road_trip_draft(rid)["saved_playlist_id"] == recipe["last_playlist_id"]
    playlist = next(p for p in store.get_playlists() if p.ytm_playlist_id == recipe["last_playlist_id"])
    assert f'window.location.assign("/playlist/{playlist.id}")' in r.text
    assert "music.youtube.com/watch?list=" + recipe["last_playlist_id"] in r.text
    assert "__ytTab" in r.text


def test_saving_plays_in_the_connected_youtube_tab_when_ready(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    client = FakeClient()
    c, _ = _app(store, client, monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    bridge = c.app.state.ctx.bridge = SimpleNamespace(connected=True)
    navigated, calls = threading.Event(), []

    def navigate(*args):
        calls.append(args)
        navigated.set()

    monkeypatch.setattr(road_trip_route.executor, "navigate_when_ready", navigate)
    response = c.post(f"/road_trip/draft/{rid}/save")
    assert response.status_code == 200
    assert navigated.wait(2)
    ytm = store.get_road_trip_recipe(rid)["last_playlist_id"]
    assert calls == [(client, bridge, ytm, f"https://music.youtube.com/watch?list={ytm}")]


@pytest.mark.parametrize("failure", ["youtube", "account", "missing"])
def test_save_failures_use_the_standard_feedback_and_leave_no_saved_link(store, monkeypatch, failure):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")
    if failure == "youtube":
        def failed(*args, **kwargs):
            raise RuntimeError("private provider details")
        monkeypatch.setattr(road_trip_route.executor, "create_generated_playlist", failed)
    elif failure == "account":
        c.app.state.ctx.client_provider = lambda: {}
    else:
        store.delete_road_trip_draft(rid)

    response = c.post(f"/road_trip/draft/{rid}/save")

    assert response.status_code == 200
    assert "Couldn't save" in response.text
    assert "__ytTab" in response.text
    assert "private provider details" not in response.text
    assert "window.location.assign" not in response.text
    assert store.get_road_trip_recipe(rid)["last_playlist_id"] is None


def test_draft_survives_a_reload_and_is_discardable(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    c, _ = _app(store, monkeypatch=monkeypatch)
    rid = _recipe(store)
    c.post(f"/road_trip/recipes/{rid}/build")

    assert "Save to YouTube" in c.get("/road_trip").text     # reopens with the draft in place

    c.request("DELETE", f"/road_trip/draft/{rid}")

    assert store.get_road_trip_draft(rid) is None
    assert "Save to YouTube" not in c.get("/road_trip").text


def test_draft_endpoints_recover_when_the_draft_is_gone(store):
    c, _ = _app(store)
    rid = _recipe(store)
    r = c.post(f"/road_trip/draft/{rid}/shuffle")
    assert r.status_code == 200
    assert "build it again" in r.text


def test_autocomplete_artists(store):
    client = FakeClient(search_results=[
        {"artist": "Tame Impala", "browseId": "UC1"}, {"artist": "Tame Impala", "browseId": "UC1"}])
    c, _ = _app(store, client)
    r = c.get("/road_trip/autocomplete/artists?q=Tame")
    assert r.status_code == 200
    assert r.json()["results"] == ["Tame Impala"]     # deduped


def test_artist_genre_lookup_feeds_the_form(store, monkeypatch):
    """Adding an artist pre-fills their genre, so the mix reaches past that one artist."""
    monkeypatch.setattr(road_trip_rec, "artist_genre",
                        lambda s, name: "Alternative Rock" if name == "Weezer" else None)
    c, _ = _app(store)

    assert c.get("/road_trip/artist_genre?name=Weezer").json() == {"genre": "Alternative Rock"}
    assert c.get("/road_trip/artist_genre?name=Nobody").json() == {"genre": ""}
    assert c.get("/road_trip/artist_genre?name=").json() == {"genre": ""}   # no key, no lookup


def test_autocomplete_artists_empty_query(store):
    c, _ = _app(store)
    assert c.get("/road_trip/autocomplete/artists?q=").json()["results"] == []


def test_road_trip_page_shows_saved_recipe_and_last_playlist_link(store):
    store.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], ["synthpop"], 240, 1000.0)
    rid = store.list_road_trip_recipes()[0]["id"]
    store.set_road_trip_last_playlist(rid, "PL_ABC")

    c, _ = _app(store)
    html = c.get("/road_trip").text

    assert "Beach Run" in html
    assert "Tame Impala" in html
    assert "synthpop" in html
    assert "60" in html                 # own_pct shown somewhere
    assert "PL_ABC" in html             # last-saved playlist link present


def test_road_trip_nav_link_present(store):
    c, _ = _app(store)
    html = c.get("/road_trip").text
    assert 'href="/road_trip"' in html
