"""Home preparation finishes its captured inputs without holding UI locks."""
import asyncio
from tests.derived_rating_support import seed_dislike
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from copy import deepcopy
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, embedding_vectors as embed, measurement, mode_surfaces, surfaces
from tests import model_support
from tests.prepared_fixture_support import prepared_template, roots
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader
from yt_playlist.rec.rec_worker import RecWorker


@pytest.fixture(scope="module")
def isolation_schema():
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


@pytest.fixture(scope="module")
def isolation_template(isolation_schema):
    with prepared_template(isolation_schema, _seed) as template:
        yield template


@pytest.fixture(params=["memory", "file"])
def database(request, tmp_path, isolation_schema, isolation_template, ranking_inputs, monkeypatch):
    path = ":memory:" if request.param == "memory" else tmp_path / "state.db"
    with isolation_template.open(path) as store:
        if request.param == 'file':
            # Preserve actual WAL commits/readers without a passive checkpoint
            # after every pool row. Checkpoint policy has independent owners.
            store.conn.execute('PRAGMA wal_autocheckpoint=0')
        store._isolation_saved_heads = deepcopy(isolation_template.result)
        reporting = reuse_reporting_reader(store, monkeypatch) if request.param == 'file' else nullcontext()
        with reporting:
            assert store._isolation_saved_heads == isolation_template.result
            assert all(head is not original for head, original in zip(
                store._isolation_saved_heads, isolation_template.result, strict=True))
            _assert_content_inputs(store, isolation_template.source)
            _saved(store)
            destinations = roots(store.conn)
            copied = 0
            for name, source in isolation_template.files.items():
                destination = destinations[name]
                assert destination != source
                for path in source.rglob("*"):
                    if path.is_file():
                        private = destination / path.relative_to(source)
                        assert private.read_bytes() == path.read_bytes()
                        assert not private.samefile(path)
                        copied += 1
            assert copied
            yield store


@pytest.fixture
def ranking_inputs(monkeypatch):
    """Supply ranker output below real snapshot calculation and publication.

    Scoring, vibe generation and PPR have independent owners. These cases keep
    actual model vectors, bucketing, temporal membership, policy capture, typed
    file staging and SQL heads while racing the captured work with UI writes.
    """
    from yt_playlist.rec import ppr

    def pools(reader, now, limit):
        library = surfaces.ForYouItem('Song', 'Artist', None, 'video', None, 0,
            'Captured ranking', key='song|artist', genre='House')
        fresh = surfaces.ForYouItem('Fresh', 'New artist', None, 'fresh', None, 0,
            'Captured discovery', key='fresh|new artist', genre='House')
        return {lane: [fresh if lane == 'fresh' else library]
                for lane in ('wheelhouse', 'explore', 'comfort', 'fresh')}

    monkeypatch.setattr(mode_surfaces, '_rank_pools', pools)
    monkeypatch.setattr(mode_surfaces.vibe_pools, 'prepare', lambda *args, **kwargs: None)
    monkeypatch.setattr(ppr, 'mode_rankings', lambda *args, **kwargs: {1: ['song|artist']})


@pytest.fixture
def _worker_store(tmp_path, isolation_schema):
    store = Store(tmp_path / "state.db")
    with closing(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(store, isolation_schema)
        yield store


def _seed(store):
    seed_publication_subjects(store, [{'key': 'song|artist', 'video_id': 'video',
        'title': 'Song', 'artist': 'Artist', 'genre': 'House'}])
    model_support.build_content_model(store)
    keys, vectors, _ = embed.load_content_vectors(store)
    store.modes.replace_modes([{"mode_id": 1, "label": "House", "families": [["house", 1]],
        "centroid": vectors[0], "size": 1, "rep_keys": keys,
        "space": embed.content_space_id(store)}], retired_ids=[], now=1)
    fresh = {'key': 'fresh|new artist', 'video_id': 'fresh', 'title': 'Fresh',
             'artist': 'New artist', 'genre': 'House'}
    seed_publication_subjects(store, [fresh], discoveries=[fresh['key']])
    publish_bundles(store, {'all': {'wheelhouse': [{'key': keys[0], 'video_id': 'video',
        'title': 'Song', 'artist': 'Artist', 'genre': 'House'}]}}, 1, fresh=[fresh])
    store._isolation_saved_heads = store.recommendations.heads()
    return store._isolation_saved_heads


def _assert_content_inputs(store, original):
    assert embed.content_space_id(store) == embed.content_space_id(original)
    assert store.rec_model.get_rec_content_vectors() == original.rec_model.get_rec_content_vectors()
    assert list(map(tuple, store.conn.execute("SELECT * FROM taste_modes ORDER BY mode_id"))) == list(
        map(tuple, original.conn.execute("SELECT * FROM taste_modes ORDER BY mode_id")))


def _saved(store):
    # One actual captured view owns this complete consistency assertion.
    with store.read_view() as reader:
        assert reader.recommendations.heads() == store._isolation_saved_heads
        assert [row['video_id'] for row in reader.recommendations.candidate_page(
            'home', 'all', 'wheelhouse', limit=4)['tracks']] == ['video']
        assert [row['video_id'] for row in reader.recommendations.candidate_page(
            'fresh', 'all', 'fresh', limit=4)['tracks']] == ['fresh']


def test_ui_read_and_write_complete_while_bundle_scorer_is_still_running(
        _worker_store, isolation_template, monkeypatch):
    store = _worker_store
    _seed(store)
    _assert_content_inputs(store, isolation_template.source)
    started = threading.Event()
    work = Activity()
    worker = RecWorker(SimpleNamespace(store=store, work=work,
        logger=logging.getLogger(__name__), now_fn=lambda: 86400), debounce_s=0)

    def slow_scorer(reader, *_args, **_kwargs):
        assert reader.conn is not store.conn
        reader.conn.create_function("query_started", 0, lambda: (started.set(), 0)[1])
        reader.conn.execute("WITH RECURSIVE slow(n) AS (VALUES(query_started()) UNION ALL "
            "SELECT n+1 FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow").fetchone()

    monkeypatch.setattr(surfaces, "for_you", slow_scorer)
    monkeypatch.setattr(worker, "_rebuild", worker._refresh_bundles)

    async def interact():
        release = store.priority.enter()
        try:
            _saved(store)
            store.settings.set_setting("ui-write", "saved")
            await asyncio.sleep(0)
            return store.settings.get_setting("ui-write")
        finally:
            release()

    with ThreadPoolExecutor(1) as ui:
        try:
            worker.trigger()
            assert started.wait(5)
            assert ui.submit(asyncio.run, interact()).result(2) == "saved"
            assert worker.busy
            work.close_admission()
            worker.shutdown()
            worker.join(3)
            assert work.wait(0)
            _saved(store)
            assert not store.conn.in_transaction
        finally:
            work.close_admission()
            worker.shutdown()
            worker.join(5)
            store.conn.close()


def test_mutation_during_calculation_finishes_and_serving_applies_current_exclusions(database, monkeypatch):
    store = database
    calculate = mode_surfaces._calculate_bundles

    def mutate(reader, now, policies):
        result = calculate(reader, now, policies)
        seed_dislike(store, "song|artist", until=None, now=now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", mutate)
    result = mode_surfaces.prepare_bundles(store, 100)
    assert result is not None and result['all']['temporal']
    assert store.recommendations.heads() != store._isolation_saved_heads
    assert not mode_surfaces.assemble_cards(store, 100, 0)
    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", calculate)
    assert mode_surfaces.prepare_bundles(store, 100) is not None
    assert "song|artist" not in {item['key'] for item in store.recommendations.candidate_page(
        'home', 'all', 'temporal', limit=4)['tracks']}


def test_bundle_failure_rolls_back_both_surfaces_and_their_policy_rows(database):
    store = database
    policies = [tuple(row) for row in store.conn.execute(
        'SELECT h.surface,g.policy_version FROM recommendation_heads h '
        'JOIN recommendation_generations g ON g.id=h.generation_id ORDER BY h.surface')]
    with store.conn:
        store.conn.execute("CREATE TRIGGER reject_fresh BEFORE INSERT ON recommendation_heads "
            "WHEN NEW.surface='fresh' BEGIN SELECT RAISE(ABORT,'rejected bundles'); END")
    with pytest.raises(sqlite3.IntegrityError, match="rejected bundles"):
        mode_surfaces.prepare_bundles(store, 100)
    _saved(store)
    # Staged policies can belong to the failed, recoverable build. Neither
    # surface may expose one until their common publication commits.
    assert [tuple(row) for row in store.conn.execute(
        'SELECT h.surface,g.policy_version FROM recommendation_heads h '
        'JOIN recommendation_generations g ON g.id=h.generation_id ORDER BY h.surface')] == policies
    assert not store.conn.in_transaction


def test_build_fingerprints_models_once_and_stamps_each_pool_with_its_ranker(database, monkeypatch):
    store = database
    calls = []
    original = measurement.serving_policy

    def policy(reader, *args, **kwargs):
        assert reader.conn is not store.conn
        calls.append(reader)
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(measurement, "serving_policy", policy)
    payload = mode_surfaces.prepare_bundles(store, 100)
    assert len(calls) == 1
    temporal = payload["all"]["temporal"][0]
    configuration = store.rec_ledger.policy_configuration(temporal['pool_policy'])
    assert configuration['ranker'] == 'temporal'


def test_bundle_publication_finishes_despite_home_views_during_calculation_and_staging(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def write_unrelated(reader, now, policies):
        result = calculate(reader, now, policies)
        database.settings.set_setting("last-library-sync", str(now))
        database.rec_ledger.record_serving_policy("concurrent-offer", {}, now)
        database.rec_ledger.bump_card_view('cards', now)
        database.modes.log_impressions(1, [('wheelhouse', 1)], now)
        return result

    stage = database.recommendations.stage
    def view_while_staging(*args, **kwargs):
        database.rec_ledger.bump_card_view('cards', 101)
        database.modes.log_impressions(2, [('explore', 1)], 101)
        return stage(*args, **kwargs)

    monkeypatch.setattr(database.recommendations, 'stage', view_while_staging)
    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", write_unrelated)
    assert mode_surfaces.prepare_bundles(database, 100) is not None
    assert database.recommendations.heads() != database._isolation_saved_heads
    policy = database.recommendations.head('home')['policy_version']
    assert database.rec_ledger.policy_configuration(policy)['modes']['offered'] == {}
    assert database.modes.impression_counts() == {1: 2}
    assert candidate_refresh.due(database, 101)


def test_radio_and_trend_updates_do_not_discard_prepared_style_pools(database, monkeypatch):
    account = database.accounts.upsert_account('Radio', 'credentials', None, True)
    calculate = mode_surfaces._calculate_bundles

    def update_playback(reader, now, policies):
        result = calculate(reader, now, policies)
        from yt_playlist.core import radio_storage
        with database.conn:
            radio_storage.save(database.conn, {'episode_id': 'unrelated-radio',
                'identity': account, 'phase': 'playing', 'position': 2, 'tracks': []},
                expected=None, now=now)
        database.rec_proposals.put_proposals("trend_rollups", {"updated": now}, now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", update_playback)
    assert mode_surfaces.prepare_bundles(database, 100) is not None
    assert database.recommendations.pool('home', '1', 'temporal') is not None
    assert database.rec_proposals.get_proposals("trend_rollups") == {"updated": 100}


def test_changed_home_genres_leave_captured_style_pools_intact_and_next_round_due(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def change_preferences(reader, now, policies):
        result = calculate(reader, now, policies)
        database.rec_proposals.put_proposals('home_genre_request',
            {'epoch': 1, 'include_genres': ['jazz']}, now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", change_preferences)
    assert mode_surfaces.prepare_bundles(database, 100) is not None
    assert database.recommendations.heads() != database._isolation_saved_heads
    assert candidate_refresh.due(database, 101)


def test_missing_pools_publish_during_reenrichment_and_import(database, monkeypatch):
    with database.conn:
        database.conn.execute("DELETE FROM recommendation_heads WHERE surface IN ('home','fresh')")
    entry_id = database.conn.execute(
        "SELECT id FROM catalog_entries WHERE video_id='video'").fetchone()[0]
    calculate = mode_surfaces._calculate_bundles

    def enrich_and_import(reader, now, policies):
        result = calculate(reader, now, policies)
        database.enrichment.apply_provider_fields(entry_id, 'musicbrainz',
            {'genre': 'Techno', 'year': '2009'}, providers=['musicbrainz'])
        database.tracks.upsert_track('imported', 'Imported song', 'Another artist', None, None)
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', enrich_and_import)
    result = mode_surfaces.prepare_bundles(database, 100)
    assert result is not None
    assert result['all']['temporal']
    assert [row['key'] for row in database.recommendations.candidate_page(
        'home', 'all', 'temporal', limit=4)['tracks']] == [row['key'] for row in result['all']['temporal']]
    # Keep attribution to the model actually used, rather than refitting under a writer lock.
    policy = result['all']['temporal'][0]['pool_policy']
    assert database.rec_ledger.policy_configuration(policy)['ranker'] == 'temporal'


def test_replacing_model_during_ranking_keeps_the_captured_publication(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def replace_model(reader, now, policies):
        result = calculate(reader, now, policies)
        with database.conn:
            database.conn.execute("DELETE FROM recording_embedding_files WHERE space='content_library'")
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', replace_model)
    assert mode_surfaces.prepare_bundles(database, 100) is not None
    assert database.recommendations.heads() != database._isolation_saved_heads
    assert database.recommendations.pool('home', '1', 'temporal')
