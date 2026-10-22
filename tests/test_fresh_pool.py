import re
import threading
import time
from types import SimpleNamespace

from tests.test_home_other_genres import seed_genres
from tests.test_mode_cards_route import _client
from yt_playlist.rec import embed, fresh_pool, mode_surfaces, road_trip_candidates, surfaces
from yt_playlist.util.matching import identity_key


def test_fresh_card_waits_for_metadata_then_renders_discoveries(store):
    keys = []
    for i in range(12):
        title, artist = f"Unenriched {i}", f"Unknown artist {i}"
        key = identity_key(title, artist)
        store.discovery.upsert_discovered_track(key, f"new{i}", title, artist,
                                      None, None, None, None, None, 100)
        keys.append(key)
    mode_surfaces.prepare_bundles(store, 200)

    with _client(store) as client:
        waiting = client.get("/home/cards")
        assert waiting.status_code == 200
        assert 'data-title="Unenriched ' not in waiting.text
        assert "Discover a fresh mix" in waiting.text

        for key in keys:
            store.discovery.set_discovered_enrichment(key, "House", None)
        fresh_pool.prepare(store, 250)
        response = client.get("/home/cards")
    assert response.status_code == 200
    assert 'data-title="Unenriched ' in response.text
    assert 'hx-post="/home/generate"' in response.text
    assert "Discover a fresh mix" not in response.text


def test_populate_searches_each_selected_genre_and_keeps_only_new_matching_songs(store, monkeypatch):
    genres = ["house", "dnb", "pop"]
    monkeypatch.setattr(fresh_pool.home_themes, "genre_options", lambda s: [
        {"family": "house", "label": "house"}, {"family": "dnb", "label": "drum & bass"},
        {"family": "pop", "label": "pop"}])
    requested = []
    def artists(store, genre, limit):
        requested.append(genre)
        return [f"{genre} artist"]
    def songs(client, kind, artist, cap, store):
        assert kind == "artist"
        return [dict(video_id=artist, title="New song", artist=artist, genre=""),
                dict(video_id="owned", title="Owned", artist="Owned artist", genre="House"),
                dict(video_id="wrong", title="Wrong", artist="Other", genre="Classical")], []
    store.tracks.upsert_track("owned", "Owned", "Owned artist", None, None)
    monkeypatch.setattr(road_trip_candidates, "genre_artists", artists)
    monkeypatch.setattr(road_trip_candidates, "other_input_songs", songs)
    encoded = []
    monkeypatch.setattr(embed, "build_discovered_content_vectors", lambda s: encoded.append(s))
    messages = []
    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: object()}, now_fn=lambda: 1000)

    assert fresh_pool.populate(ctx, genres, on_progress=messages.append) == 3
    assert requested == ["drum & bass", "house", "pop"]
    rows = store.discovery.get_discovered_tracks()
    assert {r["genre"] for r in rows} == {"house", "drum & bass", "pop"}
    assert all(r["source_browse_id"].startswith("genre:") for r in rows)
    assert encoded == [store] and messages
    # A repeat does not erase metadata already enriched on a discovered song.
    store.discovery.set_discovered_enrichment(rows[0]["identity_key"], rows[0]["genre"], "2020")
    assert fresh_pool.populate(ctx, genres) == 0
    assert store.discovery.discovered_tracks_by_keys([rows[0]["identity_key"]])[rows[0]["identity_key"]]["year"] == "2020"
    store.discovery.prune_orphan_discovered_tracks()
    assert len(store.discovery.get_discovered_tracks()) == 3


def test_populate_respects_genres_to_avoid(store, monkeypatch):
    seed_genres(store)
    requested = []
    monkeypatch.setattr(road_trip_candidates, "genre_artists", lambda s, genre, limit: requested.append(genre) or [])
    monkeypatch.setattr(embed, "build_discovered_content_vectors", lambda s: None)
    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: object()}, now_fn=lambda: 1000)
    fresh_pool.populate(ctx, avoid_genres=["jazz", "techno"])
    assert requested and not set(requested) & {"jazz", "techno"}


def test_selected_fresh_mix_balances_all_three_genres_and_requires_missing_families(store):
    from collections import Counter
    from yt_playlist.util import genre_map

    tracks = [surfaces.ForYouItem(f"Song {genre} {i}", f"Artist {genre} {i}", "", f"{genre}{i}",
                                None, 0, "", key=f"{genre}{i}|artist{i}", genre=genre)
              for genre in ("House", "Drum & Bass", "Pop") for i in range(20)]
    store.rec_surface.put_proposals("mode_bundles", {"all": {"fresh": [surfaces._item_to_fresh_dict(t) for t in tracks]}}, 1)
    chosen = fresh_pool.pick(store, 1000, ["house", "dnb", "pop"])
    assert Counter(genre_map.family(t["genre"]) for t in chosen) == {"house": 4, "dnb": 4, "pop": 4}
    tracks[:] = [t for t in tracks if t.genre == "House"]
    store.rec_surface.put_proposals("mode_bundles", {"all": {"fresh": [surfaces._item_to_fresh_dict(t) for t in tracks]}}, 2)
    assert fresh_pool.pick(store, 1000, ["house", "dnb", "pop"]) == []


def _without_fresh(store):
    seed_genres(store)
    bundles = store.rec_surface.get_proposals("mode_bundles")
    for bucket in bundles.values():
        if isinstance(bucket, dict):
            bucket.pop("fresh", None)
    store.rec_surface.put_proposals("mode_bundles", bundles, 1)


def _job_id(response):
    return re.search(r'hx-get="/home/fresh-pool/(\d+)"', response.text)[1]


def test_loading_and_choosing_genres_do_not_discover_or_rank_a_fresh_pool(store, monkeypatch):
    _without_fresh(store)
    calls = []
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *a, **k: calls.append("rank") or [])
    monkeypatch.setattr(fresh_pool, "populate", lambda *a, **k: calls.append("discover"))
    client = _client(store)
    for response in [client.get("/home/cards"),
                     client.post("/home/mix-genres", data={"genres": ["jazz", "techno"]}),
                     client.get("/home/cards"),
                     client.post("/home/breadth", data={"breadth_bias": "0.5"})]:
        assert 'class="fresh-pool-art"' in response.text
        assert 'aria-busy="false"' in response.text
        assert 'data-title="Song ' in response.text
    assert calls == []


def test_genre_selection_leaves_other_requests_responsive(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from yt_playlist.rec import mode_surfaces

    _without_fresh(store)
    started, release = threading.Event(), threading.Event()
    def assemble(*args, **kwargs):
        started.set()
        release.wait(5)
        return []
    monkeypatch.setattr(mode_surfaces, "assemble_cards", assemble)
    with _client(store) as client, ThreadPoolExecutor(max_workers=2) as requests:
        making = requests.submit(client.post, "/home/mix-genres", data={"genres": ["jazz", "techno"]})
        try:
            assert started.wait(2)
            status = requests.submit(client.get, "/openapi.json").result(timeout=2)
            assert status.status_code == 200 and not making.done()
        finally:
            release.set()
        assert making.result(timeout=2).status_code == 200


def test_prepared_fresh_tracks_honor_feedback_and_library_changes(store):
    tracks = [dict(key=identity_key(f"New {i}", f"Artist {i}"), title=f"New {i}", artist=f"Artist {i}",
                   video_id=f"v{i}", genre="House" if i < 12 else "Techno") for i in range(24)]
    store.rec_surface.put_proposals("fresh_pool", tracks, 1)
    store.tracks.upsert_track("v0", "New 0", "Artist 0", None, None)
    store.rec_model.record_feedback("for_you", tracks[1]["key"], "dismiss", now=2)
    store.rec_model.set_weight("genre:techno", 0.0, lo=0, now=2)
    chosen = fresh_pool.pick(store, 3, ["house"])
    assert len(chosen) == 10
    assert not {tracks[0]["key"], tracks[1]["key"]} & {t["key"] for t in chosen}
    assert fresh_pool.pick(store, 3, ["house", "techno"]) == []


def test_fresh_slot_starts_once_rejoins_on_refresh_and_becomes_a_scoped_mix(store, monkeypatch):
    _without_fresh(store)
    started, release = threading.Event(), threading.Event()
    calls = []
    def populate(ctx, **params):
        calls.append(params)
        params["on_progress"]("Finding fresh jazz songs…")
        started.set()
        release.wait(5)
    monkeypatch.setattr(fresh_pool, "populate", populate)
    tracks, rankings = [], []
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *a, **k: rankings.append(True) or tracks)
    client = _client(store)
    preview = client.post("/home/mix-genres", data={"genres": ["jazz", "techno"]})
    assert 'class="fresh-pool-art"' in preview.text
    assert not calls  # merely rendering the slot does not start discovery
    assert not rankings  # it must not score thousands of existing discoveries either
    response = client.post("/home/fresh-pool", data={"include_genres": ["jazz", "techno"]})
    assert started.wait(1)
    job_id = _job_id(response)
    try:
        assert f'/home/fresh-pool/{job_id}' in client.get("/home/cards").text
        second = client.post("/home/fresh-pool", data={"include_genres": ["jazz", "techno"]})
        assert _job_id(second) == job_id and len(calls) == 1
        assert calls[0]["include_genres"] == ["jazz", "techno"]
        assert 'aria-busy="true"' in response.text
        assert 'hx-trigger="every 2s"' in response.text
        assert 'hx-post="/home/generate"' not in response.text
        assert not rankings  # polling and refreshing a running job must stay cheap
        # The clicked job prepares the new cold pool without waiting for a periodic mode rebuild.
        tracks.extend(surfaces.ForYouItem(f"New {i}", f"Artist {i}", "", f"new{i}", None, 0, "",
                                        key=identity_key(f"New {i}", f"Artist {i}"),
                                        genre="Jazz" if i % 2 else "Techno")
                      for i in range(12))
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        ready = client.get(f"/home/fresh-pool/{job_id}")
        if 'class="fresh-pool-art"' not in ready.text:
            break
        time.sleep(.01)
    assert 'data-title="New ' in ready.text
    assert 'hx-post="/home/generate"' in ready.text
    assert '"include": ["jazz", "techno"]' in ready.text
    assert '"model": "fresh"' in ready.text
    assert 'hx-trigger="every 2s"' not in ready.text
    assert 'data-title="New ' in client.get("/home/cards").text
    # Stored discoveries work with a new job registry too, without rebuilding cached modes.
    assert 'data-title="New ' in _client(store).get("/home/cards").text
    assert rankings == [True]  # ready cards reuse the persisted ranking across renders/restarts


def test_failed_or_empty_search_stops_polling_and_allows_retry(store, monkeypatch):
    _without_fresh(store)
    def fail(*args, **kwargs):
        raise ValueError("Connect YouTube Music to find fresh songs.")
    monkeypatch.setattr(fresh_pool, "populate", fail)
    client = _client(store)
    response = client.post("/home/fresh-pool", data={"include_genres": ["jazz"]})
    # The background task can finish before the POST returns.
    match = re.search(r'hx-get="/home/fresh-pool/(\d+)"', response.text)
    if match:
        for _ in range(100):
            response = client.get(f"/home/fresh-pool/{match[1]}")
            if 'aria-busy="false"' in response.text:
                break
            time.sleep(.01)
    assert "Connect YouTube Music" in response.text
    assert 'hx-trigger="every 2s"' not in response.text
    assert 'aria-label="Find fresh songs"' in response.text
    assert client.post("/home/fresh-pool", data={"include_genres": ["invalid"]}).status_code == 400
    assert client.get("/home/fresh-pool/999999").status_code == 404


def test_completed_search_with_insufficient_tracks_is_retryable(store, monkeypatch):
    _without_fresh(store)
    monkeypatch.setattr(fresh_pool, "populate", lambda *args, **kwargs: 0)
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *args, **kwargs: [])
    client = _client(store)
    response = client.post("/home/fresh-pool", data={"include_genres": ["jazz"]})
    match = re.search(r'hx-get="/home/fresh-pool/(\d+)"', response.text)
    if match:
        for _ in range(100):
            response = client.get(f"/home/fresh-pool/{match[1]}")
            if 'aria-busy="false"' in response.text:
                break
            time.sleep(.01)
    assert "Not enough matching songs yet" in response.text
    assert 'hx-trigger="every 2s"' not in response.text
