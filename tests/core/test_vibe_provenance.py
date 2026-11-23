"""File-owned lineage preserves exact original evidence and transaction boundaries."""
from tests.file_custody_support import export_owner, restore_owner
from tests.store_fixture_support import file_consumer_store
import shutil
import sqlite3
import json
from contextlib import closing

import pytest

from yt_playlist.core import fao, vibe_history, vibe_provenance as owner, vibe_versions
from yt_playlist.core import vibe_models as vibe_model_files
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store

DAY = 86400


def test_rescoring_provenance_roundtrips_across_decimal_id_boundaries(tmp_path):
    files = owner.ProvenanceFiles(tmp_path)
    records, _originals = files.describe([(9, [1], [1]), (10, [2], [1])])
    staged, markers = files.stage_reserved([{**record, 'file_id': 1} for record in records])
    try:
        assert {marker[0] for marker in markers} == {9, 10}
    finally:
        for publication in staged:
            publication.close()


def evidence(store):
    for key in ('one', 'two', 'target'):
        store.tracks.upsert_track(key, key, 'Shared', '', 100)
    recordings = [r[0] for r in store.conn.execute('SELECT id FROM recordings ORDER BY id')]
    with atomic_write(store):
        artist = store.conn.execute('SELECT id FROM artists LIMIT 1').fetchone()[0]
        basis = vibe_versions.stored(store.conn, 'basis')
        measurement_version = vibe_versions.column(store.conn, 'measurements')
        prediction_version = vibe_versions.column(store.conn, 'predictions')
        mids = [store.conn.execute(f'INSERT INTO measurements(recording_id,vibe,score,support,source,{measurement_version},input_digest,measured_at) '
            "VALUES (?,'relax',.5,1,'provider_measurement',?,?,1)", (rid, basis, str(rid))).lastrowid for rid in recordings[:2]]
        pid = store.conn.execute(f'INSERT INTO predictions(recording_id,vibe,score,support,kind,{prediction_version},predicted_at) '
            "VALUES (?,'relax',.5,1,'artist',?,1)", (recordings[2], basis)).lastrowid
    return pid, mids, artist, recordings[2]


def shared_receipt(store, mids, artist, *, conditioning=None):
    from yt_playlist.rec import vibe_joint_model as joint
    rows = [{'recording': store.conn.execute('SELECT recording_id FROM measurements WHERE id=?', (mid,)).fetchone()[0],
             'measurement': mid, 'artists': [artist], 'genre': None, 'album': None,
             'source': 'provider_measurement/basis', 'version': 'basis',
             'input_digest': store.conn.execute('SELECT input_digest FROM measurements WHERE id=?', (mid,)).fetchone()[0],
             'score': .5, 'support': 1.} for mid in mids]
    value = joint.fit(rows, 'relax', fixed=joint.INITIAL)
    generation, staged = vibe_model_files.stage(store.conn, value)
    try:
        if staged:
            staged.publish()
    finally:
        if staged:
            staged.close()
    return vibe_model_files.receipt(generation, [artist], .01, .05, conditioning=conditioning), value


@pytest.mark.parametrize('boundary', ['file', 'status'])
def test_native_model_interruption_keeps_the_old_head_and_pending_record(file_consumer_store, monkeypatch, boundary):
    from yt_playlist.repos import vibe_model_file_writes
    store = file_consumer_store
    _pid, mids, artist, _rid = evidence(store)
    receipt, previous = shared_receipt(store, mids, artist)
    store.vibe_predictions.repository.install_joint_model('relax', receipt['generation'], {}, [], False)
    value = {**previous, 'diagnostics': {**previous['diagnostics'], 'replacement': True}}
    generation, staged = store.vibe_models.stage(value)
    with closing(sqlite3.connect(store._db_path)) as observer:
        assert observer.execute("SELECT CAST(owner_id AS INTEGER),file_status FROM collection_file_writes "
                                "WHERE kind='vibe-model'").fetchone() == (staged.file_id, 'pending')
    assert store.vibe_models.repository.file_reference(generation) is None
    complete = vibe_model_file_writes.complete

    def interrupted_status(conn, *args):
        complete(conn, *args)
        raise OSError('status interrupted')

    try:
        with monkeypatch.context() as patch:
            if boundary == 'file':
                patch.setattr(staged, 'publish', lambda: (_ for _ in ()).throw(OSError('file interrupted')))
            else:
                patch.setattr(vibe_model_file_writes, 'complete', interrupted_status)
            with pytest.raises(OSError, match='interrupted'):
                store.vibe_predictions.publish_joint_model('relax', generation, {'outcome': 'ready'}, mids, staged, False)
    finally:
        staged.close()
    assert store.vibe_predictions.joint_state('relax')[0] == receipt['generation']
    assert store.vibe_models.read(receipt['generation']) == previous
    assert store.conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='vibe-model'").fetchone()[0] == 'pending'
    with store.read_snapshot() as snapshot:
        generation, retry = snapshot.store.vibe_models.stage(value)
        try:
            assert snapshot.publish_ordered(lambda: store.vibe_predictions.publish_joint_model(
                'relax', generation, {'outcome': 'ready'}, mids, retry, False))
        finally:
            retry.close()
    assert store.vibe_predictions.joint_state('relax')[0] == generation
    assert store.vibe_models.read(generation) == value


def test_native_model_same_digest_replacement_keeps_pinned_ids_and_exact_backup(file_consumer_store, tmp_path):
    store = file_consumer_store
    _pid, mids, artist, _rid = evidence(store)
    receipt, value = shared_receipt(store, mids, artist)
    generation, first = store.vibe_models.stage(value)
    try:
        store.vibe_predictions.publish_joint_model('relax', generation, {'outcome': 'ready'}, mids, first, False)
    finally:
        first.close()
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        old_id = reader.vibe_models.repository.file_reference(generation)
        assert old_id == first.file_id
        generation, second = store.vibe_models.stage(value)
        try:
            store.vibe_predictions.publish_joint_model('relax', generation, {'outcome': 'ready'}, mids, second, False)
        finally:
            second.close()
        assert second.file_id != first.file_id
        assert reader.vibe_models.repository.file_reference(generation) == old_id
        assert reader.vibe_models.read(generation) == value
        assert store.vibe_models.collect(10**10, limit=128) == 0
    expected = {path: (fao.read(path).bytes, fao.read(path).info.ctime_ns)
                for path in vibe_model_files.references(store.conn)}
    assert list(expected) == [vibe_model_files.directory(store.conn) / vibe_model_files.relative(second.file_id)]
    manifest = export_owner(store.conn, vibe_model_files, tmp_path / 'native-model-backup')
    shutil.rmtree(vibe_model_files.directory(store.conn))
    restore_owner(store.conn, 'vibe-models', vibe_model_files, manifest)
    assert {path: (fao.read(path).bytes, fao.read(path).info.ctime_ns)
            for path in vibe_model_files.references(store.conn)} == expected
    assert store.vibe_models.read(generation) == value


def test_batch_read_preserves_mixed_lineage_and_bounds_schema_and_file_reads(store, monkeypatch):
    unarchived, mids, artist, rid = evidence(store)
    ready = unarchived + 1
    other_chunk = (ready // owner.CHUNK_ROWS + 1) * owner.CHUNK_ROWS + 1
    expired = other_chunk + 1
    with atomic_write(store):
        basis = vibe_versions.stored(store.conn, 'basis')
        version = vibe_versions.column(store.conn, 'predictions')
        for pid in (ready, other_chunk, expired):
            store.conn.execute(f'INSERT INTO predictions(id,recording_id,vibe,score,support,kind,{version},predicted_at) '
                "VALUES (?,?,'relax',.5,1,'artist',?,1)", (pid,rid,basis))
        owner.publish_many(store.conn, [(pid,mids,[artist]) for pid in (ready,other_chunk,expired)])
        store.conn.execute("UPDATE predictions SET provenance_state='expired' WHERE id=?", (expired,))
    available, chunk = owner.available, owner._chunk
    checks, reads = [], []
    def installed(conn):
        checks.append(True)
        return available(conn)
    def load(conn, pid):
        reads.append(pid // owner.CHUNK_ROWS)
        return chunk(conn, pid)
    monkeypatch.setattr(owner, 'available', installed)
    monkeypatch.setattr(owner, '_chunk', load)
    expected = {'inputs': mids, 'artists': [artist]}
    with store.read_view() as reader:
        assert list(owner.read_many(reader.conn, [unarchived, ready, other_chunk, expired, expired+1])) == [
            (unarchived, {'inputs': [], 'artists': []}), (ready, expected), (other_chunk, expected),
            (expired, None), (expired+1, None)]
    assert not checks
    assert reads == [ready // owner.CHUNK_ROWS, other_chunk // owner.CHUNK_ROWS]
    fao.write(owner.directory(store.conn) / owner.relative(other_chunk), b'corrupt format')
    with store.read_view() as reader, pytest.raises(ValueError):
        list(owner.read_many(reader.conn, [ready,other_chunk]))


def test_staging_preserves_existing_roots_and_roots_allocated_by_the_planner(store):
    pid, mids, artist, _rid = evidence(store)
    with atomic_write(store):
        store.conn.execute('UPDATE measurements SET used_by_model=1 WHERE id=?', (mids[0],))
    # New measurements are allocated by the planner before either those rows
    # or their roots are committed. Keep both kinds across several SQL pages.
    allocated = list(range(max(mids) + 1, max(mids) + 260))
    inputs = mids + allocated
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        staged, markers, roots = owner.stage_many(store.conn, [(pid, inputs + mids, [artist])])
        try:
            assert roots == inputs[1:]
            assert markers[0][:2] == (pid, owner.fingerprint(inputs))
            assert [row[0] for row in store.conn.execute(
                'SELECT id FROM measurements WHERE used_by_model=1')] == mids[:1]
            assert not (owner.directory(store.conn) / owner.relative(pid)).exists()
            assert len([sql for sql in statements if sql.startswith('SELECT id FROM measurements WHERE used_by_model=1 AND id IN')]) == 3
        finally:
            for publication in staged:
                publication.close()
    finally:
        store.conn.set_trace_callback(None)


@pytest.mark.parametrize('shared_model', [False, True, 'conditioned'])
def test_current_protection_expiry_snapshot_and_independent_restore(store, tmp_path, shared_model):
    pid, mids, artist, rid = evidence(store)
    inputs, expected = mids, {'inputs': mids, 'artists': [artist]}
    conditioning = {'genre': None, 'album': '42'} if shared_model == 'conditioned' else None
    if shared_model:
        inputs, value = shared_receipt(store, mids, artist, conditioning=conditioning)
        expected = {**inputs, 'inputs': mids}
    with atomic_write(store):
        owner.publish_many(store.conn, [(pid, inputs, [artist])])
        store.conn.execute("INSERT INTO recording_vibes VALUES (?,'relax',.5,1,'artist',NULL,?,?,NULL,NULL,NULL,NULL)",
                           (rid, pid, vibe_versions.stored(store.conn, 'basis')))
    assert store.retention.maintain_prediction_provenance(40 * DAY) == 0
    if shared_model:
        model_path = vibe_model_files.directory(store.conn) / vibe_model_files.relative(inputs['generation'])
        collection_at = fao.read(model_path).info.ctime_ns / 1e9 + 40 * DAY
        assert vibe_model_files.collect(store.conn, collection_at) == 0
        assert model_path.exists()
        # A physically damaged required model is an error; exact verified bytes
        # can repair it without changing any calculation's immutable identity.
        fao.write(model_path, b'broken model')
        with pytest.raises(fao.CorruptFile):
            owner.read(store.conn, pid)
        repaired, _value = shared_receipt(store, mids, artist, conditioning=conditioning)
        assert repaired == inputs
        assert owner.read(store.conn, pid) == expected
    assert vibe_history.maintain(store, 200 * DAY) == 0
    with atomic_write(store):
        store.conn.execute('DELETE FROM recording_vibes WHERE recording_id=?', (rid,))
    with store.read_view() as reader:
        assert owner.read(reader.conn, pid)['inputs'] == mids
        assert store.retention.maintain_prediction_provenance(40 * DAY) == 1
        assert owner.read(reader.conn, pid)['inputs'] == mids
        assert vibe_history.maintain(store, 200 * DAY) == 0
        assert store.conn.execute('SELECT 1 FROM predictions WHERE id=?', (pid,)).fetchone()
        if shared_model:
            assert vibe_model_files.collect(store.conn, collection_at) == 0
            assert model_path.exists()  # The SQL snapshot pins its old ready ref.
    assert owner.read(store.conn, pid) is None
    for _ in range(4):
        store.vibe_predictions.collect_provenance(40 * DAY)
    assert not (owner.directory(store.conn) / owner.relative(pid)).exists()
    assert vibe_history.maintain(store, 200 * DAY) == 1
    assert owner.read(store.conn, pid) is None
    assert store.conn.execute('SELECT count(*) FROM measurements').fetchone()[0] == len(mids)

    with atomic_write(store):
        new_pid = owner.next_id(store.conn)
        version = vibe_versions.column(store.conn, 'predictions')
        store.conn.execute(f'INSERT INTO predictions(id,recording_id,vibe,score,support,kind,{version},predicted_at) '
            "VALUES (?,?,'relax',.5,1,'artist',?,2)", (new_pid, rid, vibe_versions.stored(store.conn, 'basis')))
        owner.publish_many(store.conn, [(new_pid, inputs, [artist])])
    if shared_model:
        orphan = {**value, 'diagnostics': {**value['diagnostics'], 'unpublished': True}}
        orphan_generation, staged = vibe_model_files.stage(store.conn, orphan)
        try:
            staged.publish()
        finally:
            staged.close()
        orphan_path = vibe_model_files.directory(store.conn) / vibe_model_files.relative(orphan_generation)
        collection_at = fao.read(orphan_path).info.ctime_ns / 1e9 + 40 * DAY
        with store.read_view():
            assert vibe_model_files.collect(store.conn, collection_at) == 0
            assert orphan_path.exists()
        assert sum(vibe_model_files.collect(store.conn, collection_at) for _ in range(4)) == 1
        assert not orphan_path.exists() and model_path.exists()
        assert vibe_model_files.linked(store.conn, new_pid) == inputs['generation']
    manifest = export_owner(store.conn, owner, tmp_path / 'saved')
    models = export_owner(store.conn, vibe_model_files, tmp_path / 'saved-models') if shared_model else None
    copied = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(copied.conn)
        shutil.rmtree(owner.directory(store.conn))
        if shared_model:
            shutil.rmtree(vibe_model_files.directory(store.conn))
        restore_owner(copied.conn, 'vibe-provenance', owner, manifest)
        if shared_model:
            with pytest.raises(fao.CorruptFile, match='missing'):
                owner.read(copied.conn, new_pid)
            restore_owner(copied.conn, 'vibe-models', vibe_model_files, models)
        assert owner.read(copied.conn, new_pid) == expected
        assert not copied.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        copied.conn.close()


@pytest.mark.parametrize('prior', [False, True])
def test_changed_target_conditioning_gets_distinct_receipt_when_numeric_result_is_unchanged(store, prior):
    from yt_playlist.rec import vibe_joint_model as joint

    legacy_pid, mids, artist, rid = evidence(store)
    legacy_receipt, _value = shared_receipt(store, mids, artist)
    with atomic_write(store):
        owner.publish_many(store.conn, [(legacy_pid, legacy_receipt, [artist])])
    key = store.tracks.identity_key_for_video('target')
    track = {'evidence': None, 'artist': json.dumps([artist]), 'genre': None, 'version': 'basis'}
    fit = {'generation': legacy_receipt['generation'], 'model_version': joint.VERSION,
           'score': .5, 'support': 1., 'source': 'artist', 'recordings': 2,
           'mean_variance': .01, 'new_track_variance': .05}

    def conditioning(value):
        return {'genre': None, 'album': None, 'metadata_prior': value} if prior else {'genre': None, 'album': value}

    def publish(value):
        resolved = {key: {'relax': {**fit, 'conditioning': conditioning(value)}}}
        with store.read_snapshot() as snapshot:
            prepared = snapshot.store.vibe_predictions.prepare_publication({key: track}, resolved, [], 10)
            try:
                assert snapshot.publish_ordered(lambda: store.vibe_predictions.publish_prepared(prepared))
            finally:
                prepared.close()
        return store.conn.execute("SELECT prediction_id,score,support FROM recording_vibes "
                                  "WHERE recording_id=? AND vibe='relax'", (rid,)).fetchone()

    first_value, second_value = (.2, .8) if prior else ('first', 'second')
    first, second = publish(first_value), publish(second_value)
    assert tuple(first)[1:] == tuple(second)[1:] == (.5, 1.)
    assert first[0] != second[0]
    assert publish(second_value)[0] == second[0]
    assert owner.read(store.conn, first[0])['conditioning'] == conditioning(first_value)
    assert owner.read(store.conn, second[0])['conditioning'] == conditioning(second_value)
    assert owner.read(store.conn, legacy_pid) == {**legacy_receipt, 'inputs': mids}


def test_original_subject_validation_and_ids_survive_retirement(store):
    pid, mids, artist, rid = evidence(store)
    with pytest.raises(ValueError, match='distinct original'), atomic_write(store):
        wrong = store.conn.execute(
            "INSERT INTO measurements(recording_id,vibe,score,support,source,model_version_id,input_digest) "
            "SELECT recording_id,'release',score,support,source,model_version_id,input_digest FROM measurements WHERE id=?",
            (mids[0],)).lastrowid
        owner.publish_many(store.conn, [(pid, [wrong], [artist])])
    with pytest.raises(ValueError, match='distinct original'), atomic_write(store):
        version = vibe_versions.column(store.conn, 'measurements')
        duplicate = store.conn.execute(f'INSERT INTO measurements(recording_id,vibe,score,support,source,{version},input_digest) '
            "SELECT recording_id,vibe,score,support,source,?,input_digest FROM measurements WHERE id=?",
            (vibe_versions.stored(store.conn, 'other'), mids[0])).lastrowid
        owner.publish_many(store.conn, [(pid, [mids[0], duplicate], [artist])])
    with atomic_write(store):
        version = vibe_versions.column(store.conn, 'predictions')
        basis = vibe_versions.stored(store.conn, 'basis')
        for at in (None, 200 * DAY):
            store.conn.execute(f'INSERT INTO predictions(recording_id,vibe,score,support,kind,{version},predicted_at) '
                "VALUES (?,'relax',.5,1,'artist',?,?)", (rid, basis, at))
    highwater = owner.next_id(store.conn)
    assert vibe_history.maintain(store, 200 * DAY, limit=1) == 1
    # A new invocation resumes the persisted page and keeps unknown/recent ages.
    for _ in range(3):
        assert vibe_history.maintain(store, 200 * DAY, limit=1) == 0
    assert store.conn.execute('SELECT count(*) FROM predictions').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM measurements').fetchone()[0] == len(mids)
    assert owner.read(store.conn, pid) is None
    assert owner.next_id(store.conn) > pid
    assert owner.next_id(store.conn) == highwater


@pytest.mark.parametrize('shared_model', [False, True])
def test_periodic_preparation_stages_off_lock_and_rejects_stale_allocation(store,monkeypatch,shared_model):
    _pid,mids,artist,rid = evidence(store)
    key = store.conn.execute('SELECT identity_key FROM recordings WHERE id=?',(rid,)).fetchone()[0]
    tracks = {key:{'evidence':None,'artist':json.dumps([artist]),'genre':None,'version':'basis'}}
    resolved = {key:{'relax':{'source':'artist','score':.5,'support':1.,
                                  'inputs':{'one':mids[0],'two':mids[1]}}}}
    expected = {'inputs': mids, 'artists': [artist]}
    if shared_model:
        receipt, _value = shared_receipt(store, mids, artist)
        resolved[key]['relax'] = {'source':'artist','score':.5,'support':1.,'recordings':2,
            'model_version':'joint-control', **receipt}
        expected = {**receipt, 'inputs': mids}
    original = fao.prepare
    calls = []
    def stage(*args,**kwargs):
        assert not store._lock.held_by_current_thread
        calls.append(args[0])
        return original(*args,**kwargs)
    monkeypatch.setattr(fao,'prepare',stage)
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),5)) as plan:
            with atomic_write(store):
                version = vibe_versions.column(store.conn, 'predictions')
                store.conn.execute(f'INSERT INTO predictions(recording_id,vibe,score,support,kind,{version},predicted_at) '
                    "VALUES (?,'relax',.5,1,'artist',?,4)",(rid,vibe_versions.stored(store.conn, 'competing')))
            assert not snapshot.publish_ordered(lambda:store.vibe_predictions.publish_prepared(plan))
    assert calls
    assert not list(owner.directory(store.conn).rglob('.fao-pending-*'))
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),6)) as plan:
            monkeypatch.setattr(fao,'prepare',lambda *a,**k: pytest.fail('Compression under SQL publication'))
            assert snapshot.publish_ordered(lambda:store.vibe_predictions.publish_prepared(plan))
    current = store.conn.execute('SELECT prediction_id FROM recording_vibes WHERE recording_id=?',(rid,)).fetchone()[0]
    assert owner.read(store.conn,current)==expected
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),7)) as plan:
            assert not plan.staged and not plan.predictions
            assert snapshot.publish_ordered(lambda:store.vibe_predictions.publish_prepared(plan))
    assert store.conn.execute('SELECT prediction_id FROM recording_vibes WHERE recording_id=?',(rid,)).fetchone()[0]==current
