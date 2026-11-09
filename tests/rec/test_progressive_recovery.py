"""Recovery must work without a new sync and preserve exact model behavior."""
from dataclasses import replace
import json
import logging
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from tests.rec.test_progressive import catalog, drain, _pristine_current_schema
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import _file_consumer_store, initialize_consumer, reuse_reporting_reader
from yt_playlist.rec import content_model, embedding_vectors, progressive, taste_modes
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core import activity, content_preparation_state
from yt_playlist.core.store import Store
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.repos.prepared_content import PreparedContentRepo


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Recovery checks do not construct the HTTP app or its sync daemon."""


@pytest.fixture
def audit_library(tmp_path, _pristine_current_schema, monkeypatch, native_sql_calls):
    # This audit owns content-input recovery and aged-evidence publication.
    # Joint audio-model qualification is an independent preparation branch;
    # keep actual content capture, fitting, queues and snapshot guards here.
    bound_catalog(monkeypatch, entries=(('techno', 'Techno'), ('jazz', 'Jazz'), ('rock', 'Rock')))
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
    monkeypatch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
    with _file_consumer_store(tmp_path, _pristine_current_schema) as store:
        with reuse_reporting_reader(store, monkeypatch):
            yield store


def test_recovery_schema_input_keeps_completed_sql_and_real_guards(_pristine_current_schema):
    assert _pristine_current_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert _pristine_current_schema._revision_script
    assert not _pristine_current_schema.execute('PRAGMA foreign_key_check').fetchone()


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


def _repair_after_losing_a_queue_prefix(store):
    catalog(store, 19)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
    p = progressive.Preparation(store, budget=replace(progressive.Budget(), batch=3))
    expected = [row[0] for row in store.conn.execute('SELECT identity_key FROM recordings ORDER BY identity_key')]
    assert p.due(1000)
    assert p.repair(1000), repair_diagnostics(p)
    assert len(store.prepared_content.pending(100)) == 3
    first = store.prepared_content.pending(100)
    assert [row[0] for row in first] == expected[:3]
    # Existing queue revisions and positions survive subsequent repair slices.
    assert p.repair(1001)
    assert store.prepared_content.pending(100)[:3] == first
    assert [row[0] for row in store.prepared_content.pending(100)] == expected[:6]
    # Lose earlier work after the cursor has passed it. Completing this scan
    # must not stop the next periodic audit finding that earlier identity.
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending WHERE identity_key=?', (expected[0],))
    for _ in range(10):
        if not p._repair_needed:
            break
        p.repair(1002)
    else:
        pytest.fail('Bounded queue repair did not finish')
    assert [row[0] for row in store.prepared_content.pending(100)] == expected[1:]
    assert p._repair_after is None
    assert not p.repair(1100)
    assert p.repair(2000)
    assert [row[0] for row in store.prepared_content.pending(100)] == expected[1:] + expected[:1]
    return p


@pytest.fixture(scope='module')
def periodic_audit_home(tmp_path_factory):
    return tmp_path_factory.mktemp('periodic-audit') / 'app-home'


@pytest.fixture(scope='module')
def periodic_audit_queue(periodic_audit_home, _pristine_current_schema):
    # Queue repair has no reopen contract. Keep its original in-memory SQL
    # writer; file durability is exercised by the restart cases below.
    store = Store(':memory:')
    try:
        initialize_consumer(store, _pristine_current_schema)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(periodic_audit_home))
            patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
            patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
            patch.delenv('DEEPSEEK_API_KEY', raising=False)
            patch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: 0.0))
            patch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
            p = _repair_after_losing_a_queue_prefix(store)
        yield p
    finally:
        store.conn.close()


def test_periodic_audit_requeues_a_lost_prefix_without_reordering_existing_work(periodic_audit_queue):
    p = periodic_audit_queue
    expected = [row[0] for row in p.store.conn.execute('SELECT identity_key FROM recordings ORDER BY identity_key')]
    assert [row[0] for row in p.store.prepared_content.pending(100)] == expected[1:] + expected[:1]
    assert p._repair_after is None
    assert not p.store.conn.in_transaction


@pytest.fixture(scope='module')
def periodic_audit_inputs(periodic_audit_queue, periodic_audit_home):
    p = periodic_audit_queue
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(periodic_audit_home))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        patch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: 0.0))
        patch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
        drain(p, 2000)
    return p


def test_periodic_audit_captures_every_repaired_input(periodic_audit_inputs):
    p = periodic_audit_inputs
    assert not p.store.prepared_content.pending(100)
    assert p.store.conn.execute('SELECT count(*) FROM prepared_recording_inputs').fetchone()[0] == 19
    assert not p.store.conn.in_transaction


@pytest.fixture(scope='module')
def periodic_audit_publication(periodic_audit_inputs, periodic_audit_home):
    p = periodic_audit_inputs
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(periodic_audit_home))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        patch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: 0.0))
        patch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
        assert p.fit(2060)
    return p, p.store.rec_model.content_generation()


def test_periodic_audit_publishes_every_repaired_input(periodic_audit_publication):
    p, before = periodic_audit_publication
    assert len(before['library']) == 19
    assert not p.store.prepared_content.pending(100)
    assert not p.store.conn.in_transaction


def test_periodic_audit_repairs_a_lost_queue_in_bounded_batches(
        periodic_audit_publication, periodic_audit_home, monkeypatch, record_property):
    # Real queue scans, fitting and publication continue on the same writer.
    # Only the budget clock is controlled; deadline rejection has its own case.
    p, before = periodic_audit_publication
    store = p.store
    monkeypatch.setenv('YT_PLAYLIST_HOME', str(periodic_audit_home))
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
    record_property('work_budget_clock', 'controlled; production budget values unchanged')
    key = before['library'][0][0]
    with store.conn:
        store.conn.execute('DELETE FROM prepared_recording_inputs WHERE recording_id='
                           '(SELECT id FROM recordings WHERE identity_key=?)', (key,))
    assert not p.due(2100)
    # Nothing else is written to wake preparation. The periodic audit finds it.
    assert p.due(3000)
    assert p.repair(3000)
    assert store.prepared_content.pending(1)[0][0] == key
    assert store.rec_model.content_generation() == before
    drain(p, 3000)
    assert p.fit(3060)
    assert store.rec_model.content_generation()['library'] == before['library']


def repair_diagnostics(preparation):
    diagnostic = preparation.store.maintenance_diagnostics
    return {'phases': dict(diagnostic.phases), 'reasons': dict(diagnostic.reasons),
            'repair_needed': preparation._repair_needed,
            'next_maintenance_at': preparation.next_maintenance_at}


@pytest.mark.parametrize('expiration', ['audit', 'repair'])
def test_lost_queue_deadline_defers_without_losing_work_and_retries(store, monkeypatch, expiration):
    clock = [0.0]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    catalog(store, 6)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
    p = progressive.Preparation(store, budget=replace(progressive.Budget(), batch=3))
    assert p.budget.capture_seconds == 2  # Preserve the actual default allowance.
    if expiration == 'repair':
        assert p.due(1000)
        assert p._repair_needed
    original = PreparedContentRepo.missing_unqueued

    def expired(repo, limit=32, *, after=None):
        clock[0] += p.budget.capture_seconds + .01
        # Let the real cooperative budget check reject the repository read.
        return original(repo, limit, after=after)

    with monkeypatch.context() as patch:
        patch.setattr(PreparedContentRepo, 'missing_unqueued', expired)
        assert not p.repair(1000)
    reason = 'input-audit-budget' if expiration == 'audit' else 'input-repair-budget'
    diagnostic = store.maintenance_diagnostics
    assert diagnostic.reasons[progressive.INPUT_PHASE, reason] == 1
    assert not store.conn.in_transaction
    assert not store.prepared_content.pending(10)
    assert activity._DEADLINE.get() is None
    if expiration == 'audit':
        assert p.next_maintenance_at == 1060 and not p._repair_needed
        assert not p.repair(1059)
        retry_at = 1060
    else:
        assert p._repair_needed
        retry_at = 1001
    assert p.repair(retry_at), repair_diagnostics(p)
    first = store.prepared_content.pending(10)
    expected = [row[0] for row in store.conn.execute('SELECT identity_key FROM recordings ORDER BY identity_key')]
    assert [row[0] for row in first] == expected[:3]
    assert p.repair(retry_at + 1)
    assert store.prepared_content.pending(10)[:3] == first
    assert [row[0] for row in store.prepared_content.pending(10)] == expected


def test_audit_does_not_overwrite_a_newer_arrival(store, monkeypatch):
    ids = catalog(store, 6)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
    p = progressive.Preparation(store)
    assert p.due(1000)
    original = PreparedContentRepo.missing_unqueued
    callbacks = []

    def changed(repo, limit=32, *, after=None):
        keys = original(repo, limit, after=after)
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


def test_repair_captures_only_its_four_owner_counters_after_unrelated_key_churn(store, monkeypatch):
    catalog(store, 6)
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',
                               [(f'unrelated-{i}', 'value') for i in range(4)])
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
    expected = frozenset((table, '') for table in (
        'recordings', 'recording_redirects', 'prepared_recording_inputs', 'rec_content_pending'))
    assert progressive.REPAIR_INPUTS == expected
    assert captured and all(scope == expected for scope in captured)


@pytest.mark.parametrize('owner,invalidates', [
    ('recordings', True), ('recording_redirects', True),
    ('prepared_recording_inputs', True), ('rec_content_pending', True),
    ('settings', False), ('unresolved_preparation_inputs', False),
    ('recording_embeddings', False), ('recording_embedding_files', False),
])
def test_repair_fences_each_read_owner_and_ignores_independent_owners(store, monkeypatch, owner, invalidates):
    from yt_playlist.core import prepared_inputs
    from yt_playlist.core.preparation import embeddings as embedding_preparation

    catalog(store, 6)
    subjects = list(store.conn.execute('SELECT id,identity_key FROM recordings ORDER BY id'))
    vector = np.array([1, 0], dtype=np.float32).tobytes()
    if owner == 'recording_embedding_files':
        with store.conn:
            store.conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',
                (subjects[0][0], 'collaborative', 'basis', 2, vector, 1))
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')
    missing, enqueue = PreparedContentRepo.missing_unqueued, PreparedContentRepo.enqueue_missing
    mutations, publications, queue_after_mutation = [], [], []

    def change_after_read(repo, limit=32, *, after=None):
        keys = missing(repo, limit, after=after)
        if limit == 1:
            return keys  # The preliminary due check does not publish work.
        if owner == 'recording_embedding_files':
            assert embedding_preparation.prepare(store, limit=1) == 1
        else:
            with store.conn:
                if owner == 'recordings':
                    store.conn.execute('UPDATE recordings SET bpm=bpm+1 WHERE id=?', (subjects[0][0],))
                elif owner == 'recording_redirects':
                    store.conn.execute('INSERT INTO recording_redirects VALUES (?,?,?,?)',
                        (subjects[0][0], subjects[1][0], 'test', '{}'))
                elif owner == 'prepared_recording_inputs':
                    prepared_inputs.upsert(store.conn, subjects[0][1],
                        {'library': None, 'discovered': None}, '0'*64, 1000, 1)
                elif owner == 'rec_content_pending':
                    store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?)', (subjects[0][1],))
                elif owner == 'settings':
                    store.conn.execute("INSERT INTO settings VALUES ('unrelated-during-repair','changed')")
                elif owner == 'unresolved_preparation_inputs':
                    prepared_inputs.upsert(store.conn, 'absent|artist',
                        {'library': None, 'discovered': None}, '0'*64, 1000, 1)
                else:
                    store.conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',
                        (subjects[0][0], 'collaborative', 'basis', 2, vector, 1))
        mutations.append(owner)
        queue_after_mutation[:] = store.prepared_content.pending(10)
        return keys

    def publish(repo, keys):
        publications.append(list(keys))
        return enqueue(repo, keys)

    preparation = progressive.Preparation(store)
    with monkeypatch.context() as patch:
        patch.setattr(PreparedContentRepo, 'missing_unqueued', change_after_read)
        patch.setattr(PreparedContentRepo, 'enqueue_missing', publish)
        assert preparation.repair(1000) is not invalidates
    assert mutations == [owner]
    if invalidates:
        assert publications == []
        assert store.prepared_content.pending(10) == queue_after_mutation
        expected = missing(store.prepared_content.repository, 10)
        assert preparation.repair(1001)
        assert {key for key, _revision in store.prepared_content.pending(10)} == (
            set(expected) | {key for key, _revision in queue_after_mutation})
    else:
        assert publications == [[key for _subject, key in subjects]]
        assert len(store.prepared_content.pending(10)) == 6
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def blocked_memory_input(tmp_path_factory, _pristine_current_schema):
    path = tmp_path_factory.mktemp('memory-recovery') / 'recovery.db'
    store = Store(path)
    try:
        initialize_consumer(store, _pristine_current_schema)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(path.parent / 'app-home'))
            patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
            patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
            patch.delenv('DEEPSEEK_API_KEY', raising=False)
            patch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
            catalog(store, 12)
            # Admit all inputs, then reject the real encoder/clustering peak.
            p = progressive.Preparation(store, budget=replace(progressive.Budget(), memory_bytes=128 * 1024))
            drain(p)
            assert not p.fit(1000)
        warning = progressive.maintenance_view(store)
    finally:
        store.conn.close()
    return path, warning


def test_memory_deferral_persists_exact_budget_and_retry_inputs(blocked_memory_input):
    path, warning = blocked_memory_input
    assert warning['blocked'] and warning['reason'] == 'matrix-memory-budget'
    assert warning['next_attempt_at'] == 1900
    assert set(warning['phase_bytes']) == {'encoding', 'clustering', 'publication'}
    # Inspect the closed writer's durable state independently before reopening.
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        raw = content_preparation_state.get(conn, progressive.FIT_STATE)
        persisted = json.loads(raw)
        assert persisted['reason'] == warning['reason']
        assert persisted['next_attempt_at'] == 1900
        assert conn.execute('SELECT count(*) FROM prepared_recording_inputs').fetchone()[0] == 12


def test_memory_deferral_survives_restart_and_recovers_without_new_activity(blocked_memory_input, monkeypatch):
    monkeypatch.setattr(progressive.vibe_joint_preparation, 'qualified', lambda: False)
    store = Store(blocked_memory_input[0])
    try:
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
        store.conn.execute('DELETE FROM prepared_recording_inputs WHERE recording_id='
                           '(SELECT id FROM recordings WHERE identity_key=?)', (key,))
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
    def too_slow(_inputs, *, genre_axes=None):
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
def test_audit_repairs_missing_inputs_for_previously_published_vectors(audit_library, pool):
    store = audit_library
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
        store.conn.execute('DELETE FROM prepared_recording_inputs WHERE recording_id='
                           '(SELECT id FROM recordings WHERE identity_key=?)', (key,))
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
