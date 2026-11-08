import html
import json
import logging
import re
import threading
from copy import deepcopy
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from tests.test_home_other_genres import (_client as _home_client, genre_bundles, genre_routes,
                                         seed_genres, template_bytecode)
from tests.rec.test_home_prepared import one_menu_per_attempt
from tests.home_fixture_support import home_routes_only, returning_listener
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_bundle_subjects, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import (choice_evidence, fresh_pool, home_cache, home_cards, home_prepared,
                             library_pool, measurement, mode_surfaces, rec_params, surfaces)
from yt_playlist.util.matching import identity_key


# Share compiled templates and controlled diagnostics/tag setup with the genre
# HTTP owner. Jobs, requests, real template rendering and pool filtering stay real.
pytestmark = pytest.mark.usefixtures('genre_routes', 'one_menu_per_attempt')


@pytest.fixture(scope='module')
def genre_candidates_template(_pristine_current_schema):
    # These job cases request three families and consume a completed sibling
    # card. Other genre families and twenty candidates per lane add no state.
    def seed(source):
        bundles = genre_bundles(source, genres=['Jazz', 'Techno', 'Trance'], tracks_per_lane=4)
        # The same owned recordings can be framed by different library lanes.
        # Keep Fresh's distinct, unowned candidates and all real typed pools.
        bundles['all'] = {}
        for mode in range(3):
            bucket = bundles[str(mode)]
            for lane in mode_surfaces.CARD_SURFACES:
                if lane != 'fresh':
                    bucket[lane] = [{**track, 'lane': lane} for track in bucket['wheelhouse']]
                bundles['all'].setdefault(lane, []).extend(bucket[lane])
        seed_bundle_subjects(source, bundles)
        publish_bundles(source, bundles, 1.0)
        return bundles

    with prepared_template(_pristine_current_schema, seed) as template:
        yield template


def _client(store, *, prepare=True):
    returning_listener(store, 'mixes')
    return _home_client(store, prepare=prepare)


def two_ready_cards(store, monkeypatch, *, bundles=None):
    """The screenshot case: ready Wheelhouse and Fresh, with no assigned Catalog or Comfort."""
    if bundles is None:
        bundles = seed_genres(store)
    tracks = bundles["0"]["wheelhouse"][:12]
    completed = home_cards._proto(store, 'wheelhouse', 'More in your wheelhouse', tracks, 1000)
    completed.update(mode_id=0, ranker='cosine', recipe={
        'model': 'mode', 'mode_id': 0, 'lane': 'wheelhouse', 'journey': 'shuffle', 'dj': {}})
    monkeypatch.setattr(mode_surfaces, "assemble_cards", lambda *a, **kw: [
        {"lane": "wheelhouse", "label": "More in your wheelhouse", "mode_id": 0,
         "ranker": "cosine", "tracks": tracks}])
    # Job state and rendering consume a completed sibling card. Journey
    # construction, learning and model fingerprints have independent owners in
    # test_journeys, test_choice_evidence and test_measurement_isolation.
    monkeypatch.setattr(home_cards, '_complete_card', lambda *args: deepcopy(completed))
    monkeypatch.setattr(choice_evidence, 'present', lambda *args, **kwargs: None)
    monkeypatch.setattr(measurement, 'serving_policy', lambda owner, now, **kwargs:
        ('library-job-inputs', {'schema': 4, 'algorithm': 'library-job-fixture',
                               'genres': owner.rec_proposals.get_proposals('home_genre_request')}))
    return tracks


@pytest.fixture(scope='module')
def two_ready_menu(clicked_schema):
    def seed(store):
        returning_listener(store, 'mixes', now=1000)
        parameters = {'include_genres': ['jazz', 'techno', 'trance'], 'avoid_genres': []}
        options = [{'family': genre, 'label': genre.title()} for genre in parameters['include_genres']]
        library = [{'key': f'ready {i}|artist', 'title': f'Ready {i}', 'artist': 'Artist',
                    'video_id': f'ready-{i}', 'genre': 'Jazz'} for i in range(4)]
        fresh = [{'key': f'new {genre} {i}|fresh {genre} {i}', 'title': f'New {genre} {i}',
                  'artist': f'Fresh {genre} {i}',
                  'video_id': f'new-{genre}-{i}', 'genre': genre.title()}
                 for genre in parameters['include_genres'] for i in range(2)]
        seed_publication_subjects(store, library)
        seed_bundle_subjects(store, {}, fresh=fresh)
        publish_bundles(store, {}, 1, fresh=fresh)
        service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__),
                                       genre_options=options)
        wheelhouse = home_cards._proto(store, 'wheelhouse', 'More in your wheelhouse', library, 1000)
        wheelhouse['recipe'] = {'lane': 'wheelhouse'}
        protos = [wheelhouse, service.built_fresh(parameters, 1000)]
        by_lane = {proto['lane']: proto for proto in protos}
        slots = [{'proto': by_lane.get(lane), 'pool': None if lane in by_lane else
                  service.pool_view({**parameters, 'lane': lane, 'epoch': 0})}
                 for lane in ('wheelhouse', 'explore', 'fresh', 'comfort')]
        result = home_cards.CardsResult(protos, slots, None, None, options, [], 0, None)
        home_prepared.remember(store, None, result, 1000)
        payload = home_cache.read(store, 'home_tuned')
        home_cache.put(store, home_prepared.SURFACE, payload, 1000)
        home_cache.put(store, 'home_tuned', {}, 1000)
        home_cache.put(store, 'home_choice', {}, 1000)
        return wheelhouse

    with prepared_template(clicked_schema, seed) as template:
        yield template


def test_two_ready_menu_has_real_library_and_fresh_inputs_with_two_unbuilt_slots(two_ready_menu):
    store = two_ready_menu.source
    payload = home_cache.read(store, home_prepared.SURFACE)
    slots = payload['menus'][0]['slots']
    assert [slot['proto']['lane'] if slot['proto'] else slot['pool']['lane'] for slot in slots] == [
        'wheelhouse', 'explore', 'fresh', 'comfort']
    assert sum(bool(slot['pool']) for slot in slots) == 2
    assert len(store.rec_query.library_keys()) == 4
    assert len(fresh_pool.pick(store, 1000, ['jazz', 'techno', 'trance'])) == 6


@pytest.mark.parametrize('operation', ['select', 'refresh'])
def test_two_ready_mixes_keep_all_four_slots_without_starting_work(two_ready_menu, monkeypatch, operation):
    def unexpected(*args, **kwargs):
        raise AssertionError("Only a click may rank the missing pools")
    monkeypatch.setattr(library_pool, "prepare", unexpected)
    monkeypatch.setattr(surfaces, "explore_for_you", unexpected)
    with two_ready_menu.open(':memory:') as store:
        completed = {**two_ready_menu.result, 'mode_id': None, 'ranker': 'completed-library-selection'}
        monkeypatch.setattr(mode_surfaces, 'assemble_cards', lambda *args, **kwargs: [completed])
        monkeypatch.setattr(home_cards, '_complete_card', lambda *args: deepcopy(completed))
        monkeypatch.setattr(choice_evidence, 'present', lambda *args, **kwargs: None)
        client = _client(store, prepare=False)
        response = (client.post('/home/mix-genres', data={'genres': ['jazz', 'techno', 'trance']})
                    if operation == 'select' else client.get('/home/cards'))
        assert response.status_code == 200
        assert re.findall(r'id="gen-(wheelhouse|explore|fresh|comfort)"', response.text) == [
            "wheelhouse", "explore", "fresh", "comfort"]
        assert response.text.count('class="fresh-pool-art"') == 2
        assert 'aria-label="Build from your catalog"' in response.text
        assert 'aria-label="Build comfort listening"' in response.text
        assert 'hx-trigger="every 2s"' not in response.text
        assert not client.app.state.ctx.jobs.has_active()
        if operation == 'select':
            slots = home_cache.read(store, 'home_tuned')['menus'][0]['slots']
            assert [slot['proto']['lane'] if slot['proto'] else slot['pool']['lane'] for slot in slots] == [
                'wheelhouse', 'explore', 'fresh', 'comfort']


@pytest.fixture(scope='module')
def clicked_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'), ('techno', 'Techno'),
                                  ('trance', 'Trance'), ('classical', 'Classical'))) as schema:
        yield schema


@pytest.fixture
def clicked_catalog(tmp_path, clicked_schema, monkeypatch):
    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'), ('techno', 'Techno'),
                                        ('trance', 'Trance'), ('classical', 'Classical')))
    store = Store(tmp_path / 'clicked.db')
    with closing(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        initialize_consumer(store, clicked_schema)
        ready = {lane: [{'key': f'{lane} {i}|artist {i}', 'title': f'{lane.title()} {i}',
                         'artist': f'Artist {i}', 'video_id': f'{lane}-{i}', 'genre': genre}
                        for i in range(4)]
                 for lane, genre in [('wheelhouse', 'Jazz'), ('fresh', 'Techno')]}
        seed_publication_subjects(store, [*ready['wheelhouse'], *ready['fresh']],
                                  discoveries=[item['key'] for item in ready['fresh']])
        siblings = two_ready_cards(store, monkeypatch, bundles={'0': ready})
        # Fourteen eligible survivors still require a twelve-row card; one
        # excluded Jazz row and one Classical row exercise both filters.
        candidates = [{'key': f'catalog song {i}|catalog artist {i}',
                       'title': f'Catalog song {i}', 'artist': f'Catalog artist {i}',
                       'video_id': f'v{i}', 'genre': 'Jazz' if i < 15 else 'Classical'}
                      for i in range(16)]
        seed_publication_subjects(store, candidates)
        rows = [surfaces.ForYouItem(item['title'], item['artist'], '', item['video_id'],
                    None, 0, '', key=item['key'], genre=item['genre']) for item in candidates]
        calls = []
        def rank(reader, now, limit):
            calls.append(limit)
            return rows
        monkeypatch.setattr(surfaces, 'explore_for_you', rank)
        # The genre-control route has its own HTTP owners. Supply its completed
        # persisted request instead of rerunning and rendering that menu here.
        store.rec_ledger.refresh_card('cards', max(1, rec_params.get_param(store, 'erosion_view_cap')), 1000.)
        store.rec_proposals.put_proposals('home_genre_request', {
            'epoch': 1, 'include_genres': ['jazz', 'techno', 'trance']}, 1000.)
        parameters = {'lane': 'explore', 'epoch': 1,
                      'include_genres': ['jazz', 'techno', 'trance'], 'avoid_genres': []}
        options = [{'family': genre, 'label': genre} for genre in parameters['include_genres']]
        # A completed sibling menu is a lower-layer input to clicked-pool
        # handling. Its producers and typed publication have separate owners.
        protos = [home_cards._proto(store, lane, lane.title(), tracks, 1000.) |
                  {'recipe': {'lane': lane, 'journey': 'shuffle', 'dj': {}}}
                  for lane, tracks in ready.items()]
        service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
        by_lane = {proto['lane']: proto for proto in protos}
        slots = [{'proto': by_lane.get(lane), 'pool': None if lane in by_lane else
                  service.pool_view({**parameters, 'lane': lane}, genre_options=options)}
                 for lane in ('wheelhouse', 'explore', 'fresh', 'comfort')]
        result = home_cards.CardsResult(protos, slots, None, None, options,
                                       parameters['include_genres'], 1, None)
        home_prepared.remember(store, None, result, 1000.)
        prepared = home_cache.read(store, 'home_tuned')
        assert home_prepared._valid(prepared)
        home_cache.put(store, home_prepared.SURFACE, prepared, 1000.)
        home_cache.put(store, 'home_tuned', {}, 1000.)
        home_cache.put(store, 'home_choice', {}, 1000.)
        client = _client(store, prepare=False)
        excluded = [rows[0].key] + [item['key'] for item in siblings]
        data = {**parameters, 'exclude_keys': json.dumps(excluded)}
        yield SimpleNamespace(store=store, client=client, rows=rows, calls=calls,
                              parameters=parameters, excluded=excluded, data=data)


def test_catalog_click_leaves_requests_available_while_ranking(clicked_catalog, monkeypatch):
    case = clicked_catalog
    started, release = threading.Event(), threading.Event()
    original = surfaces.explore_for_you
    def rank(reader, now, limit):
        started.set()
        assert release.wait(3), 'Catalog ranker was not released'
        return original(reader, now, limit)
    monkeypatch.setattr(surfaces, "explore_for_you", rank)
    client = case.client
    response = client.post("/home/library-pool", data=case.data)
    job_id = re.search(r'hx-get="/home/library-pool/(\d+)"', response.text)[1]
    try:
        assert started.wait(1)
        assert 'aria-busy="true"' in response.text
        assert f'/home/library-pool/{job_id}' in client.get("/home/cards").text
    finally:
        release.set()
    assert client.app.state.ctx.work.wait(3), 'Catalog work did not finish'
    assert case.calls == [300]


def test_catalog_job_rejoins_one_worker_and_keeps_the_original_exclusions(clicked_catalog, monkeypatch):
    from yt_playlist.web.jobs import start_library_pool

    case = clicked_catalog
    started, release = threading.Event(), threading.Event()
    original = surfaces.explore_for_you
    def rank(reader, now, limit):
        started.set()
        assert release.wait(3)
        return original(reader, now, limit)
    monkeypatch.setattr(surfaces, 'explore_for_you', rank)
    ctx = case.client.app.state.ctx
    job = start_library_pool(ctx, case.parameters, case.excluded)
    try:
        assert started.wait(1)
        rejoined = start_library_pool(ctx, dict(case.parameters), ['later-view'])
        assert rejoined is job and not job.done
        assert job.exclude_keys == case.excluded
        assert ctx.jobs.latest('library_pool', case.parameters) is job
    finally:
        release.set()
    assert ctx.work.wait(3)
    assert job.done and job.error is None
    assert case.calls == [300]


@pytest.fixture
def completed_catalog_job(clicked_catalog):
    case = clicked_catalog
    library_pool.prepare(case.store, 1000., case.parameters)
    job, created = case.client.app.state.ctx.jobs.create_or_rejoin('library_pool', case.parameters)
    assert created
    job.exclude_keys = case.excluded
    job.done = True
    return case, job.id


def test_completed_catalog_job_renders_filtered_card_and_survives_app_recreation(completed_catalog_job):
    case, job_id = completed_catalog_job
    client = case.client
    ready = client.get(f"/home/library-pool/{job_id}")
    assert ready.status_code == 200
    assert 'data-title="Catalog song ' in ready.text
    assert 'data-title="Catalog song 0"' not in ready.text
    assert 'data-title="Catalog song 15"' not in ready.text
    assert ready.text.count('class="gen-row"') == 12
    recipe = json.loads(html.unescape(re.search(r"data-recipe='([^']*)'", ready.text)[1]))
    assert recipe["genre_request"] == {"kind": "chosen_genres", "include": ["jazz", "techno", "trance"]}
    # Recreate the HTTP app as in the original case. The same committed Store
    # supplies the saved menu; this does not claim database crash recovery.
    assert 'data-title="Catalog song ' in _client(case.store).get("/home/cards").text
    assert case.calls == [300]


def test_catalog_refresh_expires_job_and_rejects_the_previous_choice_window(completed_catalog_job):
    case, job_id = completed_catalog_job
    from yt_playlist.rec.home_cards import _epoch
    refreshed = case.client.post("/home/refresh-cards")
    assert refreshed.status_code == 200
    assert _epoch(case.store, 'cards') == 2
    assert f'/home/library-pool/{job_id}' not in refreshed.text
    assert library_pool.pick(case.store, 1000, {
        'lane': 'explore', 'epoch': 2, 'include_genres': ['jazz', 'techno', 'trance'],
        'avoid_genres': []}) == []
    assert case.client.post('/home/library-pool', data=case.data).status_code == 400
    assert case.calls == [300]


@pytest.mark.parametrize('failed', [False, True], ids=['empty', 'failed'])
def test_empty_and_failed_library_builds_stop_polling_and_offer_retry(store, monkeypatch, failed):
    from yt_playlist.web.jobs import SyncJobs

    finished = threading.Event()
    finish = SyncJobs._finish
    def notify_finished(jobs, job):
        finish(jobs, job)
        finished.set()
    monkeypatch.setattr(SyncJobs, '_finish', notify_finished)
    def prepare(*args, **kwargs):
        if failed:
            raise RuntimeError("provider internals")
    monkeypatch.setattr(library_pool, "prepare", prepare)
    # An explicit empty-lane job needs neither candidate generation nor an
    # already prepared menu. Its terminal fragment is the behavior under test.
    client = _client(store, prepare=False)
    response = client.post("/home/library-pool", data={"lane": "comfort", "epoch": "0"})
    assert response.status_code == 200
    assert finished.wait(1), 'Library-pool worker did not complete'
    match = re.search(r'hx-get="/home/library-pool/(\d+)"', response.text)
    if match:
        response = client.get(f"/home/library-pool/{match[1]}")
    assert response.status_code == 200
    assert 'aria-label="Build comfort listening"' in response.text
    assert 'hx-trigger="every 2s"' not in response.text
    assert "provider internals" not in response.text


def test_library_pool_rejects_invalid_requests(store, monkeypatch):
    from yt_playlist.web.routes.home_views import HomeViews

    # Request validation consumes the prepared option list. Pool production is
    # below that boundary; invalid requests must never start it.
    monkeypatch.setattr(HomeViews, 'prepared', lambda *args: {'genre_options': [
        {'family': 'jazz', 'label': 'jazz'}]})
    def unexpected(*args, **kwargs):
        raise AssertionError('Invalid input started a library-pool job')
    monkeypatch.setattr(library_pool, 'prepare', unexpected)
    client = _client(store, prepare=False)
    for data in ({"lane": "unknown", "epoch": "0"}, {"lane": "explore", "epoch": "-1"},
                 {"lane": "explore", "epoch": "x"},
                 {"lane": "explore", "epoch": "0", "include_genres": ["unknown"]},
                 {"lane": "explore", "epoch": "0", "exclude_keys": "null"}):
        assert client.post("/home/library-pool", data=data).status_code == 400
    assert client.get("/home/library-pool/99999").status_code == 404


def test_temporal_pool_keeps_its_date_band_and_live_feedback(store, genre_candidates):
    bundles = genre_candidates
    keys = []
    for i, year in enumerate([1960, 1980, 1995, 2005, 2010, 2020]):
        title, artist = f"Song {year}", f"Artist {i}"
        tid = store.tracks.upsert_track(f"v{i}", title, artist, None, None)
        store.tracks.set_track_genre(tid, "Jazz")
        store.tracks.set_track_year(tid, str(year))
        keys.append(identity_key(title, artist))
    bundles["_meta"]["year_cuts"] = [1990, 2010]
    publish_bundles(store, bundles, 1)
    parameters = {"lane": "temporal", "epoch": 1, "include_genres": ["jazz"], "avoid_genres": []}
    library_pool.prepare(store, 1, parameters)
    store.rec_model.record_feedback("for_you", keys[3], "dismiss", now=2)
    assert [t["year"] for t in library_pool.pick(store, 3, parameters)] == [1995, 2010]


def test_catalog_build_reaches_unmodeled_library_beyond_other_genres(store, monkeypatch):
    # This coordinator must request the whole catalog before genre filtering.
    # The scorer honors its limit, so a regression to the default 300-item head
    # loses every Jazz row. Real unmodeled scoring is owned by
    # test_explore.test_catalog_includes_owned_tracks_without_model_coverage;
    # click/rejoin/render/restart is owned by the skeleton-click case above.
    rows, keys = [], []
    with atomic_write(store):
        for i in range(340):
            title, artist = f"Song {i}", f"Artist {i}"
            key = identity_key(title, artist)
            genre = 'Jazz' if i >= 310 else 'Techno'
            # Only matching candidates need SQL metadata in this unit. The
            # complete library inventory is the controlled DAO result below.
            if i >= 310:
                seed_catalog_track(store, key, title, artist, video_id=f"v{i}", genre=genre)
            keys.append(key)
            rows.append(surfaces.ForYouItem(title, artist, '', f'v{i}', None,
                                           0, '', key=key, genre=genre))
    monkeypatch.setattr(type(store.rec_query), 'library_keys', lambda self: set(keys))
    limits = []
    def score(reader, now, limit):
        limits.append(limit)
        return rows[:limit]
    monkeypatch.setattr(surfaces, 'explore_for_you', score)
    parameters = {'lane': 'explore', 'epoch': 0, 'include_genres': ['jazz'], 'avoid_genres': []}

    library_pool.prepare(store, 1000, parameters)
    result = library_pool.pick(store, 1000, parameters, exclude_keys=keys[310:322])

    assert limits == [340]
    assert len(result) >= 12
    assert {track['key'] for track in result} == set(keys[322:])
    assert all(track['genre'] == 'Jazz' for track in result)
