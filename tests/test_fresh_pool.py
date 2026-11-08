import re
import logging
from tests.enrichment_support import seed_discovered_enrichment
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_home_other_genres import seed_genres
from tests.test_mode_cards_route import _client as current_client
from tests.home_fixture_support import home_routes_only, prepared_client as _prepared_client, returning_listener
from tests.home_fixture_support import publish_cards, publish_menu, without_metadata_providers
from tests.rec.test_home_prepared import one_menu_per_attempt
from tests.recommendation_support import publish_bundles, seed_bundle_subjects, seed_publication_subjects
from tests.prepared_fixture_support import prepared_template
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store, reuse_reporting_reader
from yt_playlist.core.activity import Activity
from yt_playlist.rec import embedding_vectors as embed, fresh_pool, mode_surfaces, road_trip_candidates, surfaces
from yt_playlist.util.matching import identity_key


@pytest.fixture(autouse=True)
def fresh_routes(monkeypatch, home_routes_only, one_menu_per_attempt,
                 without_metadata_providers, fresh_template_bytecode):
    from yt_playlist.rec import home_cards
    from yt_playlist.web import app as webapp

    # Fresh selection, jobs and publication stay real. Audit history has its
    # own contract checks and is independent of these discovery workflows.
    monkeypatch.setattr(home_cards.HomeCards, 'audit', lambda *args, **kwargs: None)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = fresh_template_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


def _client(store, now=1000.0):
    returning_listener(store, 'mixes', now=now)
    return current_client(store, now)


def prepared_client(store):
    # The requested Fresh worker/preparation runs explicitly in these tests.
    # Unrelated periodic learning would replace synthetic prior-listening usage.
    return _prepared_client(store, background_work=False, backend_options={'use_uvloop': True})


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
            seed_discovered_enrichment(store, key, "House", None)
        fresh_pool.prepare(store, 250)
        from yt_playlist.rec import home_prepared
        payload = home_prepared.prepare(store, 250)
        assert payload is not None
        assert all(any(p['lane'] == 'fresh' for p in menu['protos']) for menu in payload['menus'])
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

    def unexpected_full_read(*args):
        raise AssertionError('Population must read membership without hydrating discovery tracks')
    with monkeypatch.context() as patch:
        patch.setattr(type(store.discovery), 'get_discovered_tracks', unexpected_full_read)
        assert fresh_pool.populate(ctx, genres, on_progress=messages.append) == 3
    assert requested == ["drum & bass", "house", "pop"]
    rows = store.discovery.get_discovered_tracks()
    assert {axes[0] for axes in store.genres.feature_axes(r['genre'] for r in rows).values()} == {'house', 'dnb', 'pop'}
    assert all(r["source_browse_id"].startswith("genre:") for r in rows)
    assert encoded == [store] and messages
    # A repeat does not erase metadata already enriched on a discovered song.
    seed_discovered_enrichment(store, rows[0]["identity_key"], rows[0]["genre"], "2020")
    with monkeypatch.context() as patch:
        patch.setattr(type(store.discovery), 'get_discovered_tracks', unexpected_full_read)
        assert fresh_pool.populate(ctx, genres) == 0
    assert store.discovery.discovered_tracks_by_keys([rows[0]["identity_key"]])[rows[0]["identity_key"]]["year"] == "2020"
    store.discovery.prune_orphan_discovered_tracks()
    assert len(store.discovery.get_discovered_tracks()) == 3


def test_populate_respects_genres_to_avoid(store, monkeypatch):
    # Population consumes genre choices, not prepared model/candidate pools.
    monkeypatch.setattr(fresh_pool.home_themes, 'genre_options', lambda s: [
        {'family': genre, 'label': genre} for genre in ('jazz', 'techno', 'house', 'dnb')])
    requested = []
    monkeypatch.setattr(road_trip_candidates, "genre_artists", lambda s, genre, limit: requested.append(genre) or [])
    monkeypatch.setattr(embed, "build_discovered_content_vectors", lambda s: None)
    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: object()}, now_fn=lambda: 1000)
    fresh_pool.populate(ctx, avoid_genres=["jazz", "techno"])
    assert set(requested) == {'house', 'dnb'}


def test_selected_fresh_mix_balances_all_three_genres_and_requires_missing_families(store):
    from collections import Counter
    from yt_playlist.util import genre_map

    tracks = [surfaces.ForYouItem(f"Song {genre} {i}", f"Artist {genre} {i}", "", f"{genre}{i}",
                                None, 0, "", key=f"{genre}{i}|artist{i}", genre=genre)
              for genre in ("House", "Drum & Bass", "Pop") for i in range(20)]
    bundle = {"all": {"fresh": [surfaces._item_to_fresh_dict(t) for t in tracks]}}
    seed_bundle_subjects(store, bundle)
    publish_bundles(store, bundle, 1)
    chosen = fresh_pool.pick(store, 1000, ["house", "dnb", "pop"])
    assert Counter(genre_map.family(t["genre"]) for t in chosen) == {"house": 4, "dnb": 4, "pop": 4}
    tracks[:] = [t for t in tracks if t.genre == "House"]
    publish_bundles(store, {"all": {"fresh": [surfaces._item_to_fresh_dict(t) for t in tracks]}}, 2)
    assert fresh_pool.pick(store, 1000, ["house", "dnb", "pop"]) == []


def _without_fresh(store):
    # These workflows need selected Jazz/Techno offers and an empty Fresh slot.
    # Publish that state once, without seeding hundreds of unrelated subjects.
    seed_genres(store, genres=['Jazz','Techno'], tracks_per_lane=8, fresh=False)


@pytest.fixture
def prepared_fresh_home(store, fresh_home_template):
    fresh_home_template.restore(store)


def _job_id(response):
    return re.search(r'hx-get="/home/fresh-pool/(\d+)"', response.text)[1]


@pytest.fixture(scope='module')
def fresh_workflow_schema():
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz'), ('techno', 'Techno'))) as schema:
        yield schema


@pytest.fixture(scope='module')
def fresh_template_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('fresh-template-bytecode')))


@pytest.fixture
def fresh_workflow_home(fresh_workflow_menu, fresh_template_bytecode,
                        without_metadata_providers, monkeypatch, tmp_path):
    with fresh_workflow_menu.open(tmp_path / 'fresh.db') as store, reuse_reporting_reader(store, monkeypatch):
        payload = fresh_workflow_menu.result
        selected = [{**proto, 'mode_id': None, 'ranker': 'completed-library-selection'}
                    for proto in payload['menus'][0]['protos']]
        monkeypatch.setattr(mode_surfaces, 'assemble_cards', lambda *args, **kwargs: selected)
        client = prepared_client(store)
        client.app.state.ctx.templates.env.bytecode_cache = fresh_template_bytecode
        with client:
            yield store, client


@pytest.fixture(scope='module')
def fresh_workflow_menu(fresh_workflow_schema):
    from yt_playlist.rec import home_cards, home_prepared

    def seed(store):
        returning_listener(store, 'mixes', now=1000)
        payload = publish_menu(store, count=3, tracks_per_card=1, genre='Jazz', genre_options=[
            {'family': 'jazz', 'label': 'Jazz'}, {'family': 'techno', 'label': 'Techno'}])
        selected = [{**proto, 'mode_id': None, 'ranker': 'completed-library-selection'}
                    for proto in payload['menus'][0]['protos']]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mode_surfaces, 'assemble_cards', lambda *args, **kwargs: selected)
            service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__),
                genre_options=payload['genre_options'])
            return publish_cards(store, service.cards(home_cards.CardsRequest(1000, record=False)), 1000)

    with prepared_template(fresh_workflow_schema, seed) as template:
        yield template


def test_fresh_slot_starts_once_rejoins_on_refresh_and_becomes_a_scoped_mix(fresh_workflow_home, monkeypatch):
    store, client = fresh_workflow_home
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
        for track in tracks:
            store.discovery.upsert_discovered_track(track.key, track.video_id, track.title, track.artist,
                "", None, track.genre, None, None, 1000)
    finally:
        release.set()
    # This waits for the worker's real typed publication, not a page request.
    # Polling stays responsive while the background preparation is unfinished.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = client.get(f"/home/fresh-pool/{job_id}")
        if 'hx-trigger="every 2s"' not in ready.text:
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


@pytest.mark.parametrize('deferred', [False, True])
def test_fresh_workflow_job_starts_once_and_rejoins_until_publication(monkeypatch, deferred):
    from unittest.mock import Mock
    from yt_playlist.web.jobs import SyncJobs, start_fresh_pool

    started, release = threading.Event(), threading.Event()
    ctx = SimpleNamespace(work=Activity(), jobs=SyncJobs(), now_fn=lambda: 1000,
        store=SimpleNamespace(recommendations=SimpleNamespace(head=Mock(return_value={'id': 17}))),
        rec_worker=SimpleNamespace(trigger_candidate_refresh=Mock(), trigger_home_refresh=Mock()),
        logger=logging.getLogger(__name__))
    parameters = {'include_genres': ['jazz', 'techno'], 'avoid_genres': []}
    calls = []

    def populate(owner, **kwargs):
        assert owner is ctx and threading.current_thread() is not threading.main_thread()
        calls.append({key: value for key, value in kwargs.items() if key != 'on_progress'})
        kwargs['on_progress']('Finding fresh jazz songs…')
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(fresh_pool, 'populate', populate)
    prepare = Mock(return_value=None if deferred else {})
    monkeypatch.setattr(fresh_pool, 'prepare', prepare)
    job = start_fresh_pool(ctx, parameters)
    try:
        assert started.wait(1) and not job.done
        assert start_fresh_pool(ctx, parameters) is job
        assert calls == [parameters] and not prepare.called
        assert job.events[-1]['text'] == 'Finding fresh jazz songs…'
    finally:
        release.set()
        assert ctx.work.wait(2)
    prepare.assert_called_once_with(ctx.store, 1000)
    assert job.result == {'generation': 17, 'requested_at': 1000}
    assert job.done is not deferred and job.error is None
    ctx.rec_worker.trigger_home_refresh.assert_called_once_with(force=True)
    if deferred:
        assert job.outcome == 'waiting'
        ctx.rec_worker.trigger_candidate_refresh.assert_called_once_with()
        assert start_fresh_pool(ctx, parameters) is job and len(calls) == 1
    else:
        ctx.rec_worker.trigger_candidate_refresh.assert_not_called()


def test_loading_and_choosing_genres_do_not_discover_or_rank_a_fresh_pool(fresh_workflow_home, monkeypatch):
    from yt_playlist.rec import choice_evidence

    store, client = fresh_workflow_home
    calls = []
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *a, **k: calls.append("rank") or [])
    monkeypatch.setattr(fresh_pool, "populate", lambda *a, **k: calls.append("discover"))
    # Visible-choice capture has separate persistence owners. This contract
    # observes whether these ordinary controls trigger discovery or ranking.
    monkeypatch.setattr(choice_evidence, 'present', lambda *args, **kwargs: None)
    for response in [client.get("/home/cards"),
                     client.post("/home/mix-genres", data={"genres": ["jazz", "techno"]}),
                     client.get("/home/cards"),
                     client.post("/home/breadth", data={"breadth_bias": "0.5"})]:
        assert 'class="fresh-pool-art"' in response.text
        assert 'aria-busy="false"' in response.text
        assert 'data-title="Prepared ' in response.text
    assert calls == []


def test_genre_selection_leaves_other_requests_responsive(store, monkeypatch, prepared_fresh_home):
    from concurrent.futures import ThreadPoolExecutor
    from yt_playlist.rec import mode_surfaces

    client = prepared_client(store)
    started, release = threading.Event(), threading.Event()
    def assemble(*args, **kwargs):
        started.set()
        release.wait(5)
        return []
    monkeypatch.setattr(mode_surfaces, "assemble_cards", assemble)
    with client, ThreadPoolExecutor(max_workers=2) as requests:
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
    seed_bundle_subjects(store, {}, fresh=tracks)
    publish_bundles(store, {}, 1, fresh=tracks)
    store.tracks.upsert_track("v0", "New 0", "Artist 0", None, None)
    store.rec_model.record_feedback("for_you", tracks[1]["key"], "dismiss", now=2)
    store.rec_model.set_weight("genre:techno", 0.0, lo=0, now=2)
    chosen = fresh_pool.pick(store, 3, ["house"])
    assert len(chosen) == 10
    assert not {tracks[0]["key"], tracks[1]["key"]} & {t["key"] for t in chosen}
    assert fresh_pool.pick(store, 3, ["house", "techno"]) == []


def test_failed_or_empty_search_stops_polling_and_allows_retry(store, monkeypatch, prepared_fresh_home):
    def fail(*args, **kwargs):
        from yt_playlist.core.action_errors import ActionInputError
        raise ActionInputError('fresh_account_required')
    monkeypatch.setattr(fresh_pool, "populate", fail)
    client = prepared_client(store)
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


@pytest.mark.parametrize('deferred', [False, True])
def test_completed_search_with_insufficient_tracks_is_retryable(fresh_workflow_home, monkeypatch, deferred):
    from yt_playlist.rec import candidate_refresh
    store, client = fresh_workflow_home
    if deferred:
        candidate_refresh.checked(store, 940)
    monkeypatch.setattr(fresh_pool, "populate", lambda *args, **kwargs: 0)
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *args, **kwargs: [])
    # This route owns completed/deferrable empty-search handling. Ranking has
    # independent owners; retain real candidate publication and job settlement.
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', lambda *args, **kwargs: ({}, []))
    response = client.post("/home/fresh-pool", data={"include_genres": ["jazz"]})
    assert client.app.state.ctx.work.wait(20)
    match = re.search(r'hx-get="/home/fresh-pool/(\d+)"', response.text)
    if deferred:
        assert match
        job = client.app.state.ctx.jobs.get(int(match[1]))
        assert job.outcome == 'waiting' and not job.done
        waiting = client.get(f'/home/fresh-pool/{job.id}')
        assert 'Not enough matching songs yet' not in waiting.text
        assert 'hx-trigger="every 2s"' in waiting.text
        assert fresh_pool.prepare(store, 4540) is not None
    if match:
        for _ in range(100):
            response = client.get(f"/home/fresh-pool/{match[1]}")
            if 'aria-busy="false"' in response.text:
                break
            time.sleep(.01)
    assert "Not enough matching songs yet" in response.text
    assert 'hx-trigger="every 2s"' not in response.text


def test_board_refresh_checks_completed_search_before_declaring_old_slot_empty(fresh_workflow_home, monkeypatch):
    store, client = fresh_workflow_home
    response = client.post('/home/mix-genres', data={'genres': ['jazz', 'techno']})
    assert 'Discover a fresh mix' in response.text
    parameters = {'include_genres': ['jazz', 'techno'], 'avoid_genres': []}
    job, created = client.app.state.ctx.jobs.create_or_rejoin('fresh_pool', parameters)
    assert created
    tracks = []
    for index in range(12):
        title, artist = f'Newly published {index}', f'New artist {index}'
        key = identity_key(title, artist)
        tracks.append({'key': key, 'title': title, 'artist': artist, 'video_id': f'new{index}',
                       'genre': 'Jazz' if index % 2 else 'Techno'})
    seed_publication_subjects(store, tracks, discoveries=[track['key'] for track in tracks], now=1000)

    def ranked(reader, now, policies):
        fresh = policies.stamp(tracks, 'fresh')
        return {'all': {'fresh': fresh}}, fresh

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', ranked)
    assert fresh_pool.prepare(store, 1000) is not None
    job.done = True
    # The old prepared board is intentionally retained, as happens when its
    # ordinary poll wins the race against the completed pool's final poll.
    stale = client.get('/home/cards')
    assert f'hx-get="/home/fresh-pool/{job.id}"' in stale.text
    assert 'Not enough matching songs yet' not in stale.text
    final = client.get(f'/home/fresh-pool/{job.id}')
    assert final.text.count('class="gen-row"') == 12
    assert 'Save and play this mix' in final.text
    assert 'hx-trigger="every 2s"' not in final.text
    assert 'data-title="Newly published ' in client.get('/home/cards').text
