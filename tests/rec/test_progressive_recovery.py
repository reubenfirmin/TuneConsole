"""Recovery must work without a new sync and preserve exact model behavior."""
from dataclasses import replace
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from tests.rec.test_progressive import catalog, drain
from yt_playlist.rec import content_model, embedding_vectors, progressive, taste_modes
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.store import Store
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.repos.prepared_content import PreparedContentRepo


def test_preallocated_encoding_preserves_exact_vectors_and_empty_rows():
    content = {'c': ('Jazz', '1991'), 'a': ('Techno', '1984'), 'empty': (None, None)}
    audio = {'a': {'bpm': 80}, 'c': {'bpm': 140}, 'b': {'music_scale': 'major'}}
    inputs = content_model.ContentInputs(sorted(set(content) | set(audio)), content, audio)
    expected_model = content_model.build_content_model(content, audio)
    expected = []
    for key in sorted(set(content) | set(audio)):
        genre, year = content.get(key, (None, None))
        vector = content_model.encode_content(expected_model, genre, year, audio.get(key))
        if vector is not None:
            expected.append((key, vector.tobytes()))
    keys, matrix, model = content_model.build_library_content(inputs)
    assert model == expected_model
    assert [(key, matrix[i].tobytes()) for i, key in enumerate(keys)] == expected
    assert 'empty' not in keys
    assert matrix.dtype == np.float32 and matrix.flags.c_contiguous
    empty = content_model.build_library_content(content_model.ContentInputs(['empty'], {'empty': (None, None)}, {}))
    assert empty.keys == [] and empty.matrix.shape == (0, 0)


def test_matrix_reuse_preserves_exact_modes_without_decoding_again(store, monkeypatch):
    catalog(store, 96)
    inputs = embedding_vectors.content_inputs(store)
    keys, matrix, model = content_model.build_library_content(inputs)
    generation = {'model': json.dumps(model),
                  'library': [(key, matrix[i].tobytes()) for i, key in enumerate(keys)]}
    genres = {key: value[0] for key, value in inputs.content.items()}
    expected = taste_modes.prepare_modes(store, 1000, k=3, min_members=8,
                                          snapshot=generation, genres=genres)
    assert expected is not None

    def unexpected(*args, **kwargs):
        raise AssertionError('The already encoded matrix was decoded again')

    monkeypatch.setattr(embedding_vectors, 'load_content_vectors', unexpected)
    actual = taste_modes.prepare_modes(store, 1000, k=3, min_members=8,
        snapshot=generation, genres=genres, vector_space=(keys, matrix, {}))
    def serialized(prepared):
        modes, retired = prepared
        return [{**mode, 'centroid': mode['centroid'].tobytes()} for mode in modes], retired
    assert serialized(actual) == serialized(expected)


def test_periodic_audit_repairs_a_lost_queue_in_bounded_batches(store):
    catalog(store, 19)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
    p = progressive.Preparation(store, budget=replace(progressive.Budget(), batch=3))
    assert p.due(1000)
    assert p.repair(1000)
    assert len(store.prepared_content.pending(100)) == 3
    first = store.prepared_content.pending(100)
    # Existing queue revisions and positions survive subsequent repair slices.
    assert p.repair(1001)
    assert store.prepared_content.pending(100)[:3] == first
    for _ in range(10):
        if not p._repair_needed:
            break
        p.repair(1002)
    else:
        pytest.fail('Bounded queue repair did not finish')
    assert len(store.prepared_content.pending(100)) == 19
    drain(p)
    assert p.fit(1060)
    before = store.rec_model.content_generation()
    key = before['library'][0][0]
    with store.conn:
        store.conn.execute('DELETE FROM rec_prepared_content WHERE identity_key=?', (key,))
    assert not p.due(1100)
    # Nothing else is written to wake preparation. The periodic audit finds it.
    assert p.due(2000)
    assert p.repair(2000)
    assert store.prepared_content.pending(1)[0][0] == key
    assert store.rec_model.content_generation() == before
    drain(p, 2000)
    assert p.fit(2060)
    assert store.rec_model.content_generation()['library'] == before['library']


def test_audit_does_not_overwrite_a_newer_arrival(store, monkeypatch):
    ids = catalog(store, 6)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
    p = progressive.Preparation(store)
    assert p.due(1000)
    original = PreparedContentRepo.missing_unqueued
    callbacks = []

    def changed(repo, limit=32):
        keys = original(repo, limit)
        if limit > 1:
            store.tracks.set_track_genre(ids[0], 'Folk')
            callbacks.append(store.prepared_content.pending(10))
        return keys

    with monkeypatch.context() as patch:
        patch.setattr(PreparedContentRepo, 'missing_unqueued', changed)
        assert not p.repair(1000)
    assert len(callbacks) == 1
    assert store.prepared_content.pending(10) == callbacks[0]
    assert p.repair(1001)
    assert store.prepared_content.pending(10)[0] == callbacks[0][0]
    assert len(store.prepared_content.pending(10)) == 6


def test_repair_captures_only_its_five_table_counters_after_unrelated_key_churn(store, monkeypatch):
    catalog(store, 6)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',
                               [(f'unrelated-{i}', 'value') for i in range(1000)])
        store.conn.execute("DELETE FROM settings WHERE key LIKE 'unrelated-%'")
    original, captured = store._snapshot_revisions.capture, []

    def capture(dependencies=None):
        assert dependencies is not None, 'Repair copied every per-record revision'
        captured.append(frozenset(dependencies))
        return original(dependencies)

    monkeypatch.setattr(store._snapshot_revisions, 'capture', capture)
    preparation = progressive.Preparation(store)
    assert preparation.repair(1000)
    assert len(store.prepared_content.pending(10)) == 6
    assert captured[0] == progressive.REPAIR_INPUTS
    assert all(scope <= progressive.REPAIR_INPUTS for scope in captured)


def test_memory_deferral_survives_restart_and_recovers_without_new_activity(tmp_path):
    path = tmp_path / 'recovery.db'
    store = Store(path)
    store.init_schema()
    try:
        catalog(store, 12)
        p = progressive.Preparation(store, budget=replace(progressive.Budget(), memory_bytes=1))
        drain(p)
        assert not p.fit(1000)
        warning = progressive.maintenance_view(store)
        assert warning['blocked'] and warning['reason'] == 'matrix-memory-budget'
        assert warning['next_attempt_at'] == 1900
        assert set(warning['phase_bytes']) == {'encoding', 'clustering', 'publication'}
        store.conn.close()
        store = Store(path)
        store.init_schema()
        reopened = progressive.Preparation(store)
        assert not reopened.due(1899) and not reopened.fit(1899)
        assert reopened.due(1900) and reopened.fit(1900)
        assert not progressive.maintenance_view(store)['blocked']
        assert progressive.maintenance_view(store)['state'] == 'ready'
    finally:
        store.conn.close()


def test_worker_polls_for_lost_input_work_without_a_catalog_write(store, monkeypatch):
    catalog(store, 12)
    p = progressive.Preparation(store)
    drain(p)
    assert p.fit(1000)
    key = store.rec_model.content_generation()['library'][0][0]
    with store.conn:
        store.conn.execute('DELETE FROM rec_prepared_content WHERE identity_key=?', (key,))
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 2000,
                                      logger=logging.getLogger(__name__)), debounce_s=0)
    scheduled = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: scheduled.append(kwargs))
    worker._poll_refresh()
    assert {'full': False, 'prepare': True} in scheduled
    assert worker.preparation.repair(2000)
    assert store.prepared_content.pending(1)[0][0] == key


def test_basis_fit_can_publish_while_independent_source_and_vibe_work_remain_due(store, monkeypatch):
    catalog(store, 12)
    p = progressive.Preparation(store)
    drain(p)
    monkeypatch.setattr(progressive.musical_preparation, 'due', lambda store: True)
    monkeypatch.setattr(progressive.vibe_preparation, 'due', lambda store: True)
    assert not store.prepared_content.has_pending()
    assert p.due(1000)
    assert p.fit(1000)
    assert p.status()['library_vectors'] == 12
    assert p.due(1000)  # Independent work remains scheduled after basis publication.


def test_row_limit_keeps_all_inputs_and_last_good_model_until_retry(store):
    ids = catalog(store, 12)
    p = progressive.Preparation(store)
    drain(p)
    assert p.fit(1000)
    for tid in ids[:4]:
        store.tracks.set_track_genre(tid, 'Folk')
    drain(p, 1001)
    before = store.rec_model.content_generation()
    p.budget = replace(p.budget, max_rows=5)
    assert not p.fit(1060)
    assert progressive.maintenance_view(store)['reason'] == 'corpus-memory-budget'
    assert store.rec_model.content_generation() == before
    assert p.status()['prepared'] == 12
    p.budget = progressive.Budget()
    assert not p.due(1959)
    assert p.due(1960) and p.fit(1960)
    assert not progressive.maintenance_view(store)['blocked']


def test_input_publication_does_not_clear_a_blocked_basis_wait(store):
    ids = catalog(store, 12)
    p = progressive.Preparation(store, budget=replace(progressive.Budget(), memory_bytes=1))
    drain(p)
    assert not p.fit(1000)
    assert progressive.PHASE in store.maintenance_diagnostics.waiting
    store.tracks.set_track_genre(ids[0], 'Folk')
    drain(p, 1001)
    assert store.maintenance_diagnostics.phases[progressive.INPUT_PHASE, 'published']
    assert progressive.PHASE in store.maintenance_diagnostics.waiting
    assert progressive.maintenance_view(store)['blocked']


def test_repeated_time_budget_failure_is_visible_and_retried(store, monkeypatch):
    catalog(store, 12)
    p = progressive.Preparation(store)
    drain(p)
    def too_slow(_inputs):
        raise WorkBudgetExceeded()
    with monkeypatch.context() as patch:
        patch.setattr(content_model, 'build_library_content', too_slow)
        for now in (1000, 1060, 1120):
            assert not p.fit(now)
    status = progressive.maintenance_view(store)
    assert status['blocked'] and status['attempts'] == 3
    assert status['next_attempt_at'] == 2020
    assert not p.due(2019)
    assert p.due(2020) and p.fit(2020)
    assert not progressive.maintenance_view(store)['blocked']


@pytest.mark.parametrize('pool', ['library', 'discovered'])
def test_audit_repairs_missing_inputs_for_previously_published_vectors(store, pool):
    catalog(store, 12)
    if pool == 'discovered':
        store.discovery.upsert_discovered_track('guest|guest', 'guest', 'Guest', 'Guest', '', None,
                                               'Techno', None, None, 1000)
    p = progressive.Preparation(store)
    drain(p)
    assert p.fit(1000)
    generation = store.rec_model.content_generation()
    key = generation[pool][0][0]
    with store.conn:
        store.conn.execute('DELETE FROM rec_prepared_content WHERE identity_key=?', (key,))
        store.conn.execute('DELETE FROM rec_content_pending WHERE identity_key=?', (key,))
    reopened = progressive.Preparation(store)
    assert reopened.due(1060)
    assert reopened.repair(1060)
    assert store.prepared_content.pending(1)[0][0] == key
    assert not reopened.fit(1060)
    assert store.rec_model.content_generation() == generation
    drain(reopened, 1060)
    assert not store.prepared_content.has_pending()
    # One repaired input is below the two-change threshold for this corpus.
    # With no new writes, the original basis reaches its aged-evidence deadline.
    assert not reopened.fit(1120)
    assert not reopened.fit(1899)
    assert store.rec_model.content_generation() == generation
    assert reopened.due(1900) and reopened.fit(1900)
    after = store.rec_model.content_generation()
    assert after['library'] == generation['library']
    assert after['discovered'] == generation['discovered']
    assert reopened.status()['basis']['reason'] == 'aged-evidence'
    assert progressive.maintenance_view(store)['state'] == 'ready'
