import html
import json
import re
import threading
import time

from tests.test_home_other_genres import seed_genres
from tests.test_mode_cards_route import _client
from yt_playlist.rec import library_pool, mode_surfaces, surfaces
from yt_playlist.util.matching import identity_key


def two_ready_cards(store, monkeypatch):
    """The screenshot case: ready Wheelhouse and Fresh, with no assigned Catalog or Comfort."""
    seed_genres(store)
    tracks = store.rec_surface.get_proposals("mode_bundles")["0"]["wheelhouse"][:12]
    monkeypatch.setattr(mode_surfaces, "assemble_cards", lambda *a, **kw: [
        {"lane": "wheelhouse", "label": "More in your wheelhouse", "mode_id": 0,
         "ranker": "cosine", "tracks": tracks}])
    return tracks


def test_two_ready_mixes_keep_all_four_slots_without_starting_work(store, monkeypatch):
    two_ready_cards(store, monkeypatch)
    def unexpected(*args, **kwargs):
        raise AssertionError("Only a click may rank the missing pools")
    monkeypatch.setattr(library_pool, "prepare", unexpected)
    monkeypatch.setattr(surfaces, "explore_for_you", unexpected)
    client = _client(store)
    for response in (client.post("/home/mix-genres", data={"genres": ["jazz", "techno", "trance"]}),
                     client.get("/home/cards")):
        assert response.status_code == 200
        assert re.findall(r'id="gen-(wheelhouse|explore|fresh|comfort)"', response.text) == [
            "wheelhouse", "explore", "fresh", "comfort"]
        assert response.text.count('class="fresh-pool-art"') == 2
        assert 'aria-label="Build from your catalog"' in response.text
        assert 'aria-label="Build comfort listening"' in response.text
        assert 'hx-trigger="every 2s"' not in response.text


def test_catalog_skeleton_click_ranks_once_preserves_genres_and_survives_restart(store, monkeypatch):
    siblings = two_ready_cards(store, monkeypatch)
    started, release = threading.Event(), threading.Event()
    rows = []
    for i in range(30):
        title, artist = f"Catalog song {i}", f"Catalog artist {i}"
        key, genre = identity_key(title, artist), "Jazz" if i < 15 else "Classical"
        tid = store.tracks.upsert_track(f"v{i}", title, artist, None, None)
        store.tracks.set_track_genre(tid, genre)
        rows.append(surfaces.ForYouItem(title, artist, "", f"v{i}", None, 0, "", key=key, genre=genre))
    calls = []
    def rank(store, now, limit):
        calls.append(limit)
        started.set()
        release.wait(5)
        return rows
    monkeypatch.setattr(surfaces, "explore_for_you", rank)
    client = _client(store)
    client.post("/home/mix-genres", data={"genres": ["jazz", "techno", "trance"]})
    parameters = {"lane": "explore", "epoch": "1", "include_genres": ["jazz", "techno", "trance"]}
    data = {**parameters, "exclude_keys": json.dumps([rows[0].key] + [t["key"] for t in siblings])}
    response = client.post("/home/library-pool", data=data)
    assert started.wait(1)
    job_id = re.search(r'hx-get="/home/library-pool/(\d+)"', response.text)[1]
    try:
        assert 'aria-busy="true"' in response.text
        assert f'/home/library-pool/{job_id}' in client.get("/home/cards").text
        again = client.post("/home/library-pool", data=data)
        assert f'/home/library-pool/{job_id}' in again.text
        assert len(calls) == 1
        assert client.get("/openapi.json").status_code == 200
    finally:
        release.set()
    for _ in range(200):
        ready = client.get(f"/home/library-pool/{job_id}")
        if 'class="fresh-pool-art"' not in ready.text:
            break
        time.sleep(.01)
    assert 'data-title="Catalog song ' in ready.text
    assert 'data-title="Catalog song 0"' not in ready.text
    assert 'data-title="Catalog song 15"' not in ready.text
    assert ready.text.count('class="gen-row"') == 12
    recipe = json.loads(html.unescape(re.search(r"data-recipe='([^']*)'", ready.text)[1]))
    assert recipe["genre_request"] == {"kind": "chosen_genres", "include": ["jazz", "techno", "trance"]}
    assert 'data-title="Catalog song ' in client.get("/home/cards").text
    assert 'data-title="Catalog song ' in _client(store).get("/home/cards").text
    assert len(calls) == 1
    # A new menu cannot reuse the preceding selection's cached pool.
    assert 'data-title="Catalog song ' not in client.post("/home/refresh-cards").text


def test_empty_and_failed_library_builds_stop_polling_and_offer_retry(store, monkeypatch):
    seed_genres(store)
    def fail(*args, **kwargs):
        raise RuntimeError("provider internals")
    for prepare in (lambda *a: None, fail):
        monkeypatch.setattr(library_pool, "prepare", prepare)
        client = _client(store)
        response = client.post("/home/library-pool", data={"lane": "comfort", "epoch": "0"})
        match = re.search(r'hx-get="/home/library-pool/(\d+)"', response.text)
        if match:
            for _ in range(200):
                response = client.get(f"/home/library-pool/{match[1]}")
                if 'aria-busy="false"' in response.text:
                    break
                time.sleep(.01)
        assert 'aria-label="Build comfort listening"' in response.text
        assert 'hx-trigger="every 2s"' not in response.text
        assert "provider internals" not in response.text


def test_library_pool_rejects_invalid_requests(store):
    seed_genres(store)
    client = _client(store)
    for data in ({"lane": "unknown", "epoch": "0"}, {"lane": "explore", "epoch": "-1"},
                 {"lane": "explore", "epoch": "x"},
                 {"lane": "explore", "epoch": "0", "include_genres": ["unknown"]},
                 {"lane": "explore", "epoch": "0", "exclude_keys": "null"}):
        assert client.post("/home/library-pool", data=data).status_code == 400
    assert client.get("/home/library-pool/99999").status_code == 404


def test_temporal_pool_keeps_its_date_band_and_live_feedback(store):
    seed_genres(store)
    keys = []
    for i, year in enumerate([1960, 1980, 1995, 2005, 2010, 2020]):
        title, artist = f"Song {year}", f"Artist {i}"
        tid = store.tracks.upsert_track(f"v{i}", title, artist, None, None)
        store.tracks.set_track_genre(tid, "Jazz")
        store.tracks.set_track_year(tid, str(year))
        keys.append(identity_key(title, artist))
    bundles = store.rec_surface.get_proposals("mode_bundles")
    bundles["_meta"]["year_cuts"] = [1990, 2010]
    store.rec_surface.put_proposals("mode_bundles", bundles, 1)
    parameters = {"lane": "temporal", "epoch": 1, "include_genres": ["jazz"], "avoid_genres": []}
    library_pool.prepare(store, 1, parameters)
    store.rec_model.record_feedback("for_you", keys[3], "dismiss", now=2)
    assert [t["year"] for t in library_pool.pick(store, 3, parameters)] == [1995, 2010]


def test_catalog_build_reaches_unmodeled_library_beyond_other_genres(store):
    # A large library need not have vectors for every track. The requested genre is deep in
    # the catalog, and sibling cards already use some of it; neither condition makes it empty.
    keys = []
    for i in range(340):
        title, artist = f"Song {i}", f"Artist {i}"
        tid = store.tracks.upsert_track(f"v{i}", title, artist, None, None)
        store.tracks.set_track_genre(tid, "Jazz" if i >= 310 else "Techno")
        keys.append(identity_key(title, artist))
    parameters = {"lane": "explore", "epoch": "0", "include_genres": ["jazz"],
                  "exclude_keys": json.dumps(keys[310:322])}
    client = _client(store)
    response = client.post("/home/library-pool", data=parameters)
    for _ in range(200):
        match = re.search(r'hx-get="(/home/library-pool/\d+)"', response.text)
        if not match:
            break
        response = client.get(match[1])
        time.sleep(.01)
    assert response.status_code == 200
    assert response.text.count('class="gen-row"') == 12
    assert "Not enough matching songs" not in response.text
    for title in re.findall(r'data-title="Song (\d+)"', response.text):
        assert 322 <= int(title) < 340
