import html
import json
import re
import threading

import pytest

from tests.conftest import seed_catalog_track
from tests.test_home_other_genres import _client as _home_client, genre_routes, seed_genres, template_bytecode
from tests.rec.test_home_prepared import one_menu_per_attempt
from tests.home_fixture_support import home_routes_only, returning_listener
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import library_pool, mode_surfaces, surfaces
from yt_playlist.util.matching import identity_key


# Share compiled templates and controlled diagnostics/tag setup with the genre
# HTTP owner. Jobs, requests, real template rendering and pool filtering stay real.
pytestmark = pytest.mark.usefixtures('genre_routes', 'one_menu_per_attempt')


@pytest.fixture(scope='module')
def genre_candidates_template(_pristine_current_schema):
    # These job cases request three families and consume a completed sibling
    # card. Other genre families and twenty candidates per lane add no state.
    with prepared_template(_pristine_current_schema, lambda source: seed_genres(
            source, genres=['Jazz', 'Techno', 'Trance'], tracks_per_lane=4)) as template:
        yield template


def _client(store, *, prepare=True):
    returning_listener(store, 'mixes')
    return _home_client(store, prepare=prepare)


def two_ready_cards(store, monkeypatch, *, bundles=None):
    """The screenshot case: ready Wheelhouse and Fresh, with no assigned Catalog or Comfort."""
    if bundles is None:
        bundles = seed_genres(store)
    tracks = bundles["0"]["wheelhouse"][:12]
    monkeypatch.setattr(mode_surfaces, "assemble_cards", lambda *a, **kw: [
        {"lane": "wheelhouse", "label": "More in your wheelhouse", "mode_id": 0,
         "ranker": "cosine", "tracks": tracks}])
    return tracks


def test_two_ready_mixes_keep_all_four_slots_without_starting_work(store, monkeypatch, genre_candidates):
    two_ready_cards(store, monkeypatch, bundles=genre_candidates)
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


def test_catalog_skeleton_click_ranks_once_preserves_genres_and_survives_restart(store, monkeypatch, genre_candidates):
    siblings = two_ready_cards(store, monkeypatch, bundles=genre_candidates)
    started, release = threading.Event(), threading.Event()
    rows = []
    with atomic_write(store):
        for i in range(30):
            title, artist = f"Catalog song {i}", f"Catalog artist {i}"
            key, genre = identity_key(title, artist), "Jazz" if i < 15 else "Classical"
            seed_catalog_track(store, key, title, artist, video_id=f"v{i}", genre=genre)
            rows.append(surfaces.ForYouItem(title, artist, "", f"v{i}", None, 0, "", key=key, genre=genre))
    calls = []
    def rank(store, now, limit):
        calls.append(limit)
        started.set()
        assert release.wait(5), 'Catalog ranker was not released'
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
    assert client.app.state.ctx.work.wait(5), 'Catalog work did not finish'
    ready = client.get(f"/home/library-pool/{job_id}")
    assert 'data-title="Catalog song ' in ready.text
    assert 'data-title="Catalog song 0"' not in ready.text
    assert 'data-title="Catalog song 15"' not in ready.text
    assert ready.text.count('class="gen-row"') == 12
    recipe = json.loads(html.unescape(re.search(r"data-recipe='([^']*)'", ready.text)[1]))
    assert recipe["genre_request"] == {"kind": "chosen_genres", "include": ["jazz", "techno", "trance"]}
    assert 'data-title="Catalog song ' in client.get("/home/cards").text
    assert 'data-title="Catalog song ' in _client(store).get("/home/cards").text
    assert len(calls) == 1
    # Restart preparation can legitimately publish these songs in a new menu.
    # The preceding job/cache must still be scoped to its original choice window.
    from yt_playlist.rec.home_cards import _epoch
    refreshed = client.post("/home/refresh-cards")
    assert refreshed.status_code == 200
    assert _epoch(store, 'cards') == 2
    assert f'/home/library-pool/{job_id}' not in refreshed.text
    assert library_pool.pick(store, 1000, {
        'lane': 'explore', 'epoch': 2, 'include_genres': ['jazz', 'techno', 'trance'],
        'avoid_genres': []}) == []
    assert client.post('/home/library-pool', data=data).status_code == 400
    assert len(calls) == 1


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
