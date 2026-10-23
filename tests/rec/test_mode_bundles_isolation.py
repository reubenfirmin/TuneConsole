"""Home preparation must not hold UI locks or publish results from superseded inputs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.rec import embedding_vectors as embed, measurement, mode_surfaces, surfaces
from tests import model_support
from yt_playlist.rec.rec_worker import RecWorker


@pytest.fixture(params=["memory", "file"])
def database(request, tmp_path):
    store = Store(":memory:" if request.param == "memory" else tmp_path / "state.db")
    store.init_schema()
    _seed(store)
    yield store
    store.conn.close()


def _seed(store):
    tid = store.tracks.upsert_track("video", "Song", "Artist", None, None)
    store.tracks.set_track_genre(tid, "House")
    model_support.build_content_model(store)
    keys, vectors, _ = embed.load_content_vectors(store)
    store.modes.replace_modes([{"mode_id": 1, "label": "House", "families": [["house", 1]],
        "centroid": vectors[0], "size": 1, "rep_keys": keys,
        "space": embed.content_space_id(store)}], retired_ids=[], now=1)
    store.rec_proposals.put_proposals("mode_bundles", {"saved": True}, 1)
    store.rec_proposals.put_proposals("fresh_pool", [{"saved": True}], 1)


def test_ui_read_and_write_complete_while_bundle_scorer_is_still_running(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    _seed(store)
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
            assert store.rec_proposals.get_proposals("mode_bundles") == {"saved": True}
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
            assert store.rec_proposals.get_proposals("mode_bundles") == {"saved": True}
            assert store.rec_proposals.get_proposals("fresh_pool") == [{"saved": True}]
            assert not store.conn.in_transaction
        finally:
            work.close_admission()
            worker.shutdown()
            worker.join(5)
            store.conn.close()


def test_mutation_during_calculation_preserves_saved_bundles_and_retries(database, monkeypatch):
    store = database
    calculate = mode_surfaces._calculate_bundles

    def mutate(reader, now, policies):
        result = calculate(reader, now, policies)
        store.rec_model.record_dislike("song|artist", until=None, now=now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", mutate)
    assert mode_surfaces.prepare_bundles(store, 100) is None
    assert store.rec_proposals.get_proposals("mode_bundles") == {"saved": True}
    assert store.rec_proposals.get_proposals("fresh_pool") == [{"saved": True}]
    assert not store.conn.execute("SELECT 1 FROM rec_serving_policies").fetchone()
    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", calculate)
    assert mode_surfaces.prepare_bundles(store, 100) is not None
    payload = store.rec_proposals.get_proposals("mode_bundles")
    assert "song|artist" not in {item["key"] for item in payload["all"]["temporal"]}


def test_bundle_failure_rolls_back_both_surfaces_and_their_policy_rows(database):
    store = database
    with store.conn:
        store.conn.execute("CREATE TRIGGER reject_fresh BEFORE INSERT ON rec_proposals "
            "WHEN NEW.surface='fresh_pool' BEGIN SELECT RAISE(ABORT,'rejected bundles'); END")
    with pytest.raises(sqlite3.IntegrityError, match="rejected bundles"):
        mode_surfaces.prepare_bundles(store, 100)
    assert store.rec_proposals.get_proposals("mode_bundles") == {"saved": True}
    assert store.rec_proposals.get_proposals("fresh_pool") == [{"saved": True}]
    assert not store.conn.execute("SELECT 1 FROM rec_serving_policies").fetchone()
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
    configuration = store.conn.execute("SELECT configuration FROM rec_serving_policies "
        "WHERE model_version=?", (temporal["pool_policy"],)).fetchone()[0]
    assert '"ranker":"temporal"' in configuration


def test_bundle_publication_survives_unrelated_watermarks_and_policy_writes(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def write_unrelated(reader, now, policies):
        result = calculate(reader, now, policies)
        database.settings.set_setting("last-library-sync", str(now))
        database.rec_ledger.record_serving_policy("concurrent-offer", {}, now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", write_unrelated)
    assert mode_surfaces.prepare_bundles(database, 100) is not None
    assert database.rec_proposals.get_proposals("mode_bundles") != {"saved": True}


def test_radio_and_trend_updates_do_not_discard_prepared_style_pools(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def update_playback(reader, now, policies):
        result = calculate(reader, now, policies)
        database.settings.set_setting("radio_current_session", '{"position":2}')
        database.rec_proposals.put_proposals("trend_rollups", {"updated": now}, now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", update_playback)
    assert mode_surfaces.prepare_bundles(database, 100) is not None
    assert "1" in database.rec_proposals.get_proposals("mode_bundles")
    assert database.rec_proposals.get_proposals("trend_rollups") == {"updated": 100}


def test_changed_home_genres_still_discard_prepared_style_pools(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def change_preferences(reader, now, policies):
        result = calculate(reader, now, policies)
        database.rec_proposals.put_proposals("home_genre_request", {"include": ["jazz"]}, now)
        return result

    monkeypatch.setattr(mode_surfaces, "_calculate_bundles", change_preferences)
    assert mode_surfaces.prepare_bundles(database, 100) is None
    assert database.rec_proposals.get_proposals("mode_bundles") == {"saved": True}


def test_missing_pools_publish_during_reenrichment_and_import(database, monkeypatch):
    with database.conn:
        database.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('mode_bundles','fresh_pool')")
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
    assert database.rec_proposals.get_proposals('mode_bundles') == result
    # Keep attribution to the model actually used, rather than refitting under a writer lock.
    policy = result['all']['temporal'][0]['pool_policy']
    assert database.conn.execute('SELECT 1 FROM rec_serving_policies WHERE model_version=?',
                                 (policy,)).fetchone()


def test_replacing_model_during_ranking_still_rejects_publication(database, monkeypatch):
    calculate = mode_surfaces._calculate_bundles

    def replace_model(reader, now, policies):
        result = calculate(reader, now, policies)
        with database.conn:
            database.conn.execute('DELETE FROM rec_content_vectors')
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', replace_model)
    assert mode_surfaces.prepare_bundles(database, 100) is None
    assert database.rec_proposals.get_proposals('mode_bundles') == {'saved': True}
