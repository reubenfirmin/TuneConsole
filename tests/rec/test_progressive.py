"""Progress without a quiet catalog, using guarded, resumable inputs."""
from dataclasses import replace
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import seed_catalog_track
from yt_playlist.core import catalog as catalog_writes
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import content_model, embedding_training, embedding_vectors, model_refresh, rec_params
from yt_playlist.rec import musical_preparation, vibe_preparation
from yt_playlist.rec.progressive import BASIS_STATE, Budget, Preparation
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.worker_queue import Job
from yt_playlist.repos.prepared_content import PreparedContentRepo


def catalog(store, n=12):
    ids = []
    for i in range(n):
        tid = seed_catalog_track(store, f'song {i:04}|artist {i}', f'Song {i:04}',
                                 f'Artist {i}', video_id=f'v{i}',
                                 genre=('Techno', 'Jazz', 'Rock')[i % 3],
                                 bpm=80 + i, energy=(i % 10) / 10)
        ids.append(tid)
    return ids


def audio(store, tid, bpm):
    # Re-enrichment may replace a selected finding. The provider convenience
    # setter is deliberately fill-only and would leave an existing BPM intact.
    with store.conn:
        store.conn.execute('UPDATE catalog_tracks SET bpm=? WHERE id=?', (bpm, tid))


def drain(preparation, now=1000):
    store = preparation.store
    for _ in range(500):
        if (not store.prepared_content.has_pending() and not musical_preparation.due(store)
                and not vibe_preparation.due(store)):
            return
        assert preparation.capture(now)
    pytest.fail('Input preparation did not drain')


def test_empty_then_single_recording_can_prepare_without_coverage_gate(store):
    p = Preparation(store)
    drain(p)  # Complete version/queue bootstrap before checking an idle library.
    assert not p.due(1000) and not p.fit(1000)
    tid = store.tracks.upsert_track('one', 'One', 'Artist', None, None)
    assert p.capture(1000)
    assert not p.fit(1000)
    assert not store.settings.get_setting('rec_content_model')
    store.tracks.set_track_genre(tid, 'Techno')
    assert p.capture(1060) and p.fit(1060)
    assert p.status()['library_vectors'] == 1
    assert not store.modes.list_modes(active_only=True)


def test_unrelated_enrichment_does_not_discard_captured_batch(store, monkeypatch):
    ids = catalog(store)
    p = Preparation(store, budget=replace(Budget(), batch=3))
    capture = PreparedContentRepo.capture

    def write_elsewhere(repo, pending):
        rows = capture(repo, pending)
        audio(store, ids[-1], 200)
        return rows

    monkeypatch.setattr(PreparedContentRepo, 'capture', write_elsewhere)
    assert p.capture(1000)
    assert p.status()['prepared'] == 3
    assert p.status()['pending'] == 9


def test_hot_recording_yields_without_losing_its_pending_revision(store, monkeypatch):
    ids = catalog(store, 3)
    p = Preparation(store, budget=replace(Budget(), batch=1))
    capture = PreparedContentRepo.capture

    def write_same(repo, pending):
        rows = capture(repo, pending)
        audio(store, ids[0], 200)
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(PreparedContentRepo, 'capture', write_same)
        assert not p.capture(1000)
    assert p.status()['prepared'] == 0 and p.status()['pending'] == 3
    assert p.capture(1001)
    assert store.prepared_content.pending(3)[-1][0] == store.tracks.identity_key_for_video('v0')
    drain(p)
    assert p.status()['prepared'] == 3


def test_basis_publishes_accepted_inputs_while_live_metadata_keeps_changing(store, monkeypatch):
    ids = catalog(store, 96)
    rec_params.set_param(store, 'modes_k', 4)
    rec_params.set_param(store, 'modes_min_members', 10)
    p = Preparation(store)
    drain(p)
    build = content_model.build_library_content
    writes = []

    def enrich_during_fit(inputs):
        for i, tid in enumerate(ids):
            audio(store, tid, 300 + i)
            writes.append(tid)
        return build(inputs)

    monkeypatch.setattr(content_model, 'build_library_content', enrich_during_fit)
    assert p.fit(1000)
    assert len(writes) == 96 and p.status()['pending'] == 96
    assert p.status()['library_vectors'] == 96
    assert store.modes.list_modes(active_only=True)
    assert json.loads(store.settings.get_setting('rec_content_model'))['cont'][0][1] < 200


def test_delta_uses_exact_published_basis_and_preserves_other_vectors(store):
    ids = catalog(store, 20)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    before = store.rec_model.content_generation()
    key = store.tracks.identity_key_for_video('v0')
    audio(store, ids[0], 190)
    assert p.capture(1001)
    after = store.rec_model.content_generation()
    assert after['model'] == before['model']
    assert after['generation'] != before['generation']
    assert not after['inputs']
    old, new = dict(before['library']), dict(after['library'])
    assert {k: v for k, v in old.items() if k != key} == {k: v for k, v in new.items() if k != key}
    expected = content_model.encode_content(json.loads(after['model']), 'Techno', None,
                                           {'bpm': 190, 'energy': 0})
    np.testing.assert_array_equal(np.frombuffer(new[key], np.float32), expected)


def test_pending_and_accepted_work_survive_restart_and_timestamp_only_reenrichment(tmp_path):
    path = tmp_path / 'library.db'
    store = Store(path)
    store.init_schema()
    ids = catalog(store, 6)
    p = Preparation(store, budget=replace(Budget(), batch=2))
    assert p.capture(1000)
    before = p.status()
    store.conn.close()
    store = Store(path)
    store.init_schema()
    try:
        p = Preparation(store)
        assert p.status()['pending'] == before['pending'] == 4
        assert p.status()['prepared'] == before['prepared'] == 2
        drain(p)
        assert p.fit(1000)
        generation = store.rec_model.content_generation()
        with store.conn:
            store.conn.execute('UPDATE recordings SET last_enriched_at=1100')
        assert not p.due(1100)
        assert store.rec_model.content_generation() == generation
        # A provider re-read with the same values retains every accepted row.
        store.tracks.set_track_enrichment(ids[0], 'Techno', None)
        assert not p.due(1101)
    finally:
        store.conn.close()


def test_drain_does_not_swallow_deferred_fit_or_aged_small_update(store, monkeypatch):
    ids = catalog(store, 20)
    p = Preparation(store)
    drain(p)
    build = content_model.build_library_content
    monkeypatch.setattr(content_model, 'build_library_content',
                        lambda _: (_ for _ in ()).throw(WorkBudgetExceeded()))
    assert not p.fit(1000) and not p.due(1059)
    monkeypatch.setattr(content_model, 'build_library_content', build)
    assert p.due(1060) and p.fit(1060)
    store.tracks.set_track_genre(ids[0], 'Folk')
    assert p.capture(1061)
    assert not p.due(1120)
    assert p.due(1960) and p.fit(1960)
    assert json.loads(store.settings.get_setting(BASIS_STATE))['reason'] == 'aged-evidence'


@pytest.mark.parametrize('failure', ['time', 'memory', 'cancel', 'competing-publisher'])
def test_deferred_or_cancelled_fit_keeps_last_good_generation(store, monkeypatch, failure):
    ids = catalog(store)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    for tid in ids[:4]:
        store.tracks.set_track_genre(tid, 'Classical')
    drain(p)
    before = store.rec_model.content_generation()
    build = content_model.build_library_content
    if failure == 'memory':
        p.budget = replace(p.budget, memory_bytes=1)
    else:
        def fail(inputs):
            if failure == 'competing-publisher':
                store.settings.set_setting('rec_content_generation', 'newer')
                return build(inputs)
            raise WorkBudgetExceeded() if failure == 'time' else WorkClosing()
        monkeypatch.setattr(content_model, 'build_library_content', fail)
    if failure == 'cancel':
        with pytest.raises(WorkClosing):
            p.fit(1060)
    else:
        assert not p.fit(1060)
    after = store.rec_model.content_generation()
    if failure == 'competing-publisher':
        before['generation'] = 'newer'
    assert after == before


def test_larger_fit_waits_for_accumulated_changes_or_aged_evidence(store):
    ids = catalog(store, 20)
    p = Preparation(store)
    drain(p)
    assert model_refresh.rebuild(store, 1000)
    p.completed_full(p.full_checkpoint(1000))
    assert not p.full_due(1000)
    for _ in range(5):
        audio(store, ids[0], 200 + _)
        assert p.capture(1001)
    assert not p.full_due(1001)  # five writes to one entity are still one changed entity
    assert p.full_due(1900)
    for tid in ids[1:4]:
        store.tracks.set_track_genre(tid, 'Classical')
    drain(p)
    assert p.full_due(1002)


def test_worker_polls_basis_retry_without_new_writes(store, monkeypatch):
    catalog(store, 1)
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 1060,
                                      logger=logging.getLogger(__name__)), debounce_s=0)
    drain(worker.preparation)
    scheduled = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: scheduled.append(kwargs))
    worker._poll_refresh()
    assert {'full': False, 'prepare': True} in scheduled
    assert worker.jobs.retry[Job.PREPARE].minimum_interval == 5


@pytest.mark.parametrize('repair', ['model', 'genre-revision'])
def test_stable_corpus_can_repair_missing_or_obsolete_encoder(store, repair):
    catalog(store)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    store.settings.delete_setting('rec_content_model' if repair == 'model' else 'rec_content_genre_revision')
    assert p.due(1060) and p.fit(1060)
    assert p.status()['library_vectors'] == 12


def test_bootstrap_never_replaces_a_larger_published_cohort_with_partial_inputs(store):
    catalog(store, 12)
    assert model_refresh.rebuild(store, 1000)
    before = store.rec_model.content_generation()
    p = Preparation(store, budget=replace(Budget(), batch=2))
    assert p.capture(1000)
    assert not p.fit(1000)
    assert store.rec_model.content_generation() == before
    drain(p)
    assert p.fit(1060)
    assert p.status()['library_vectors'] == 12


def test_removed_library_membership_retracts_its_content_vector(store):
    ids = catalog(store, 12)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    key = store.tracks.identity_key_for_video('v0')
    with store.conn:
        store.conn.execute('DELETE FROM library_memberships WHERE entry_id=?', (ids[0],))
    assert p.capture(1001)
    assert key not in dict(store.rec_model.content_generation()['library'])


def test_external_writer_still_invalidates_capture(tmp_path, monkeypatch):
    import sqlite3
    store = Store(tmp_path / 'external.db')
    store.init_schema()
    store.conn.execute('PRAGMA journal_mode=WAL')
    catalog(store, 2)
    capture = PreparedContentRepo.capture
    def external(repo, pending):
        rows = capture(repo, pending)
        with sqlite3.connect(store._db_path) as writer:
            writer.execute("UPDATE recordings SET bpm=200 WHERE identity_key='song 0001|artist 1'")
        return rows
    monkeypatch.setattr(PreparedContentRepo, 'capture', external)
    try:
        assert not Preparation(store).capture(1000)
        assert store.prepared_content.progress()['pending'] == 2
    finally:
        store.conn.close()


def test_full_fit_memory_limit_preserves_previous_publication_and_pending_work(store):
    ids = catalog(store, 96)
    assert model_refresh.rebuild(store, 1000)
    before = store.rec_model.content_generation()
    store.tracks.set_track_genre(ids[0], 'Classical')
    with pytest.raises(model_refresh.MemoryBudgetExceeded):
        model_refresh.rebuild(store, 1001, memory_bytes=1)
    assert store.rec_model.content_generation() == before
    assert store.rec_refresh.has_pending_changes()


def test_grouping_settings_are_not_held_behind_metadata_accumulation(store):
    catalog(store, 96)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    assert model_refresh.rebuild(store, 1000)
    p.completed_full(p.full_checkpoint(1000))
    assert not p.due(1060) and not p.full_due(1060)
    rec_params.set_param(store, 'modes_k', 2)
    assert p.due(1060) and p.full_due(1060)
    assert p.fit(1060)
    assert json.loads(store.settings.get_setting(BASIS_STATE))['reason'] == 'grouping-settings'


def test_deferred_full_fit_reuses_exact_svd_inputs_then_retrains_on_changed_baskets(store, monkeypatch):
    from tests.rec.test_model_refresh import seed
    ids = seed(store)
    cache = model_refresh.FitCache()
    train = embedding_training.build_vectors_from_baskets
    build = content_model.build_library_content
    calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs['method'])
        return train(*args, **kwargs)
    def changed_after_svd(inputs):
        audio(store, ids[0], 300 + len(calls))
        return build(inputs)
    monkeypatch.setattr(embedding_training, 'build_vectors_from_baskets', counted)
    with monkeypatch.context() as patch:
        patch.setattr(content_model, 'build_library_content', changed_after_svd)
        assert model_refresh.rebuild(store, 1000, fit_cache=cache) is None
        audio(store, ids[0], 400)
        assert model_refresh.rebuild(store, 1001, fit_cache=cache) is None
    assert calls == ['svd']
    assert model_refresh.rebuild(store, 1002, fit_cache=cache)
    assert calls == ['svd']
    store.tracks.set_track_genre(ids[0], 'Classical')
    assert model_refresh.rebuild(store, 1003, fit_cache=cache)
    assert calls == ['svd', 'svd']
    assert store.rec_model.rec_vectors_count() == 96


def test_unresolved_metadata_capture_notifies_full_models_without_eager_rebuild(store):
    tid = seed_catalog_track(store, 'song|artist', 'Song', 'Artist', genre='Techno')
    p = Preparation(store)
    drain(p)
    assert model_refresh.rebuild(store, 1000)
    p.completed_full(p.full_checkpoint(1000))
    with store.conn:
        store.conn.execute("UPDATE unresolved_items SET mb_year='1995' WHERE entry_id=?", (tid,))
    assert not store.rec_refresh.has_pending_changes()
    assert p.capture(1001)
    assert store.rec_refresh.has_pending_changes()
    assert not p.full_due(1001) and p.full_due(1900)


def test_reassociation_retracts_old_identity_and_matches_full_duplicate_entry_aggregation(store):
    ids = catalog(store, 2)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    old, target = [store.tracks.identity_key_for_video(f'v{i}') for i in range(2)]
    rid = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (ids[1],)).fetchone()[0]
    with store._lock, store.conn:
        catalog_writes.associate_item(store.conn, 'v0', rid, source='verified',
                                      evidence={'review': 'same recording'}, now=1001)
    # Keep two selected entry values; the complete model uses MIN aggregation.
    store.tracks.set_track_genre(ids[0], '')
    store.tracks.set_track_year(ids[1], '1995')
    drain(p, 1001)
    current = dict(store.rec_model.content_generation()['library'])
    assert old not in current and target in current
    assert p.fit(1060)
    expected = content_model.build_library_content(embedding_vectors.content_inputs(store))
    assert store.rec_model.content_generation()['library'] == [
        (key, expected.matrix[i].tobytes()) for i, key in enumerate(expected.keys)]


def test_discovery_delta_preserves_separate_pool_provenance(store):
    catalog(store, 12)
    p = Preparation(store)
    drain(p)
    assert p.fit(1000)
    before = dict(store.rec_model.content_generation()['library'])
    key = store.discovery.upsert_discovered_track('guest|guest', 'guest', 'Guest', 'Guest', '', None,
                                                 'Techno', None, None, 1001)
    drain(p, 1001)
    generation = store.rec_model.content_generation()
    assert key in dict(generation['discovered']) and key not in dict(generation['library'])
    assert dict(generation['library']) == before
    with store.conn:
        store.conn.execute('DELETE FROM discovery_memberships WHERE recording_id='
                           '(SELECT id FROM recordings WHERE identity_key=?)', (key,))
    drain(p, 1002)
    assert key not in dict(store.rec_model.content_generation()['discovered'])
    assert dict(store.rec_model.content_generation()['library']) == before
