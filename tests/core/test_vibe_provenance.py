"""File-owned lineage preserves exact original evidence and transaction boundaries."""
import shutil
import sqlite3
import json
from contextlib import closing

import pytest

from yt_playlist.core import fao, vibe_history, vibe_provenance as owner, vibe_versions
from yt_playlist.repos import vibe_models as vibe_model_files
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import vibe_provenance as preparation
from yt_playlist.core.store import Store

DAY = 86400


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


def shared_receipt(store, mids, artist):
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
    return vibe_model_files.receipt(generation, [artist], .01, .05), value


def test_partial_conversion_exact_reuse_and_failed_publication(store, monkeypatch):
    pid, mids, artist, rid = evidence(store)
    with atomic_write(store):
        store.conn.executemany('INSERT INTO prediction_inputs VALUES (?,?)', [(pid, mid) for mid in mids])
        store.conn.execute('INSERT INTO prediction_artists VALUES (?,?)', (pid, artist))
    expected = {'inputs': mids, 'artists': [artist]}
    assert owner.read(store.conn, pid) == expected
    with monkeypatch.context() as patch:
        patch.setattr(fao, 'write_bytes', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError, match='disk full'):
            preparation.prepare(store, limit=1)
    assert owner.read(store.conn, pid) == expected
    assert store.conn.execute('SELECT count(*) FROM prediction_inputs').fetchone()[0] == 2
    assert preparation.prepare(store, limit=1) == 1
    assert preparation.prepare(store, limit=1) == 0
    assert owner.read(store.conn, pid) == expected
    assert owner.same_inputs(store.conn, pid, list(reversed(mids)))
    assert not owner.same_inputs(store.conn, pid, mids[:1])
    assert not store.conn.execute('SELECT 1 FROM prediction_inputs').fetchone()
    assert not store.conn.execute('SELECT 1 FROM prediction_artists').fetchone()
    path = owner.directory(store.conn) / owner.relative(pid)
    fao.write_bytes(path, b'corrupt format')
    with pytest.raises(ValueError):
        owner.read(store.conn, pid)


def test_batch_read_preserves_mixed_lineage_and_bounds_schema_and_file_reads(store, monkeypatch):
    legacy, mids, artist, rid = evidence(store)
    ready = legacy + 1
    other_chunk = (ready // owner.CHUNK_ROWS + 1) * owner.CHUNK_ROWS + 1
    expired = other_chunk + 1
    with atomic_write(store):
        store.conn.executemany('INSERT INTO prediction_inputs VALUES (?,?)', [(legacy, mid) for mid in mids])
        store.conn.execute('INSERT INTO prediction_artists VALUES (?,?)', (legacy, artist))
        basis = vibe_versions.stored(store.conn, 'basis')
        version = vibe_versions.column(store.conn, 'predictions')
        for pid in (ready, other_chunk, expired):
            store.conn.execute(f'INSERT INTO predictions(id,recording_id,vibe,score,support,kind,{version},predicted_at) '
                "VALUES (?,?,'relax',.5,1,'artist',?,1)", (pid,rid,basis))
        owner.publish_many(store.conn, [(pid,mids,[artist]) for pid in (ready,other_chunk,expired)])
        store.conn.execute("UPDATE prediction_provenance SET state='expired' WHERE prediction_id=?", (expired,))
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
        assert list(owner.read_many(reader.conn, [legacy,ready,other_chunk,expired,expired+1])) == [
            (legacy,expected), (ready,expected), (other_chunk,expected), (expired,None), (expired+1,None)]
    assert len(checks) == 1
    assert reads == [ready // owner.CHUNK_ROWS, other_chunk // owner.CHUNK_ROWS]
    fao.write_bytes(owner.directory(store.conn) / owner.relative(other_chunk), b'corrupt format')
    with store.read_view() as reader, pytest.raises(ValueError):
        list(owner.read_many(reader.conn, [ready,other_chunk]))


@pytest.mark.parametrize('shared_model', [False, True])
def test_current_protection_expiry_snapshot_and_independent_restore(store, tmp_path, shared_model):
    pid, mids, artist, rid = evidence(store)
    inputs, expected = mids, {'inputs': mids, 'artists': [artist]}
    if shared_model:
        inputs, value = shared_receipt(store, mids, artist)
        expected = {**inputs, 'inputs': mids}
    with atomic_write(store):
        owner.publish_many(store.conn, [(pid, inputs, [artist])])
        store.conn.execute("INSERT INTO recording_vibes VALUES (?,'relax',.5,1,'artist',NULL,?,?,NULL,NULL,NULL,NULL)",
                           (rid, pid, vibe_versions.stored(store.conn, 'basis')))
    assert store.retention.maintain_prediction_provenance(40 * DAY) == 0
    if shared_model:
        model_path = vibe_model_files.directory(store.conn) / vibe_model_files.relative(inputs['generation'])
        collection_at = fao.stat(model_path).ctime_ns / 1e9 + 40 * DAY
        assert vibe_model_files.collect(store.conn, collection_at) == 0
        assert model_path.exists()
        # A physically damaged required model is an error; exact verified bytes
        # can repair it without changing any calculation's immutable identity.
        fao.write_bytes(model_path, b'broken model')
        with pytest.raises(fao.CorruptFile):
            owner.read(store.conn, pid)
        repaired, _value = shared_receipt(store, mids, artist)
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
    owner.collect(store.conn, 40 * DAY)
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
        collection_at = fao.stat(orphan_path).ctime_ns / 1e9 + 40 * DAY
        with store.read_view():
            assert vibe_model_files.collect(store.conn, collection_at) == 0
            assert orphan_path.exists()
        assert sum(vibe_model_files.collect(store.conn, collection_at) for _ in range(4)) == 1
        assert not orphan_path.exists() and model_path.exists()
        assert vibe_model_files.linked(store.conn, new_pid) == inputs['generation']
    manifest = owner.export(store.conn, tmp_path / 'saved')
    models = vibe_model_files.export(store.conn, tmp_path / 'saved-models') if shared_model else None
    copied = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(copied.conn)
        shutil.rmtree(owner.directory(store.conn))
        if shared_model:
            shutil.rmtree(vibe_model_files.directory(store.conn))
        owner.restore(copied.conn, manifest)
        if shared_model:
            with pytest.raises(fao.CorruptFile, match='missing'):
                owner.read(copied.conn, new_pid)
            vibe_model_files.restore(copied.conn, models)
        assert owner.read(copied.conn, new_pid) == expected
        assert not copied.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        copied.conn.close()


def test_original_subject_validation_and_ids_survive_retirement(store):
    pid, mids, artist, rid = evidence(store)
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
    original = fao.stage_bytes
    calls = []
    def stage(*args,**kwargs):
        assert not store._lock.held_by_current_thread
        calls.append(args[0])
        return original(*args,**kwargs)
    monkeypatch.setattr(fao,'stage_bytes',stage)
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),5)) as plan:
            with atomic_write(store):
                version = vibe_versions.column(store.conn, 'predictions')
                store.conn.execute(f'INSERT INTO predictions(recording_id,vibe,score,support,kind,{version},predicted_at) '
                    "VALUES (?,'relax',.5,1,'artist',?,4)",(rid,vibe_versions.stored(store.conn, 'competing')))
            assert not snapshot.publish(lambda:store.vibe_predictions.publish_prepared(plan))
    assert calls
    assert not list(owner.directory(store.conn).rglob('.fao-pending-*'))
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),6)) as plan:
            monkeypatch.setattr(fao,'stage_bytes',lambda *a,**k: pytest.fail('Compression under SQL publication'))
            assert snapshot.publish(lambda:store.vibe_predictions.publish_prepared(plan))
    current = store.conn.execute('SELECT prediction_id FROM recording_vibes WHERE recording_id=?',(rid,)).fetchone()[0]
    assert owner.read(store.conn,current)==expected
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),7)) as plan:
            assert not plan.staged and not plan.predictions
            assert snapshot.publish(lambda:store.vibe_predictions.publish_prepared(plan))
    assert store.conn.execute('SELECT prediction_id FROM recording_vibes WHERE recording_id=?',(rid,)).fetchone()[0]==current
