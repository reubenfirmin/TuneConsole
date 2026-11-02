"""File-owned lineage preserves exact original evidence and transaction boundaries."""
import shutil
import sqlite3
import json
from contextlib import closing

import pytest

from yt_playlist.core import fao, vibe_history, vibe_provenance as owner
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
        mids = [store.conn.execute('INSERT INTO measurements(recording_id,vibe,score,support,source,model_version,input_digest,measured_at) '
            "VALUES (?,'relax',.5,1,'provider_measurement','basis',?,1)", (rid, str(rid))).lastrowid for rid in recordings[:2]]
        pid = store.conn.execute('INSERT INTO predictions(recording_id,vibe,score,support,kind,policy_version,predicted_at) '
            "VALUES (?,'relax',.5,1,'artist','basis',1)", (recordings[2],)).lastrowid
    return pid, mids, artist, recordings[2]


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
        for pid in (ready, other_chunk, expired):
            store.conn.execute('INSERT INTO predictions(id,recording_id,vibe,score,support,kind,policy_version,predicted_at) '
                "VALUES (?,?,'relax',.5,1,'artist','basis',1)", (pid,rid))
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


def test_current_protection_expiry_snapshot_and_independent_restore(store, tmp_path):
    pid, mids, artist, rid = evidence(store)
    with atomic_write(store):
        owner.publish_many(store.conn, [(pid, mids, [artist])])
        store.conn.execute("INSERT INTO recording_vibes VALUES (?,'relax',.5,1,'artist',NULL,?,'basis',NULL,NULL,NULL,NULL)", (rid, pid))
    assert preparation.maintain(store, 40 * DAY) == 0
    assert vibe_history.maintain(store, 200 * DAY) == 0
    with atomic_write(store):
        store.conn.execute('DELETE FROM recording_vibes WHERE recording_id=?', (rid,))
    with store.read_view() as reader:
        assert owner.read(reader.conn, pid)['inputs'] == mids
        assert preparation.maintain(store, 40 * DAY) == 1
        assert owner.read(reader.conn, pid)['inputs'] == mids
        assert vibe_history.maintain(store, 200 * DAY) == 0
        assert store.conn.execute('SELECT 1 FROM predictions WHERE id=?', (pid,)).fetchone()
    assert owner.read(store.conn, pid) is None
    owner.collect(store.conn, 40 * DAY)
    assert not (owner.directory(store.conn) / owner.relative(pid)).exists()
    assert vibe_history.maintain(store, 200 * DAY) == 1
    assert owner.read(store.conn, pid) is None
    assert store.conn.execute('SELECT count(*) FROM measurements').fetchone()[0] == len(mids)

    with atomic_write(store):
        new_pid = owner.next_id(store.conn)
        store.conn.execute('INSERT INTO predictions(id,recording_id,vibe,score,support,kind,policy_version,predicted_at) '
            "VALUES (?,?,'relax',.5,1,'artist','basis',2)", (new_pid, rid))
        owner.publish_many(store.conn, [(new_pid, mids, [artist])])
    manifest = owner.export(store.conn, tmp_path / 'saved')
    copied = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(copied.conn)
        shutil.rmtree(owner.directory(store.conn))
        owner.restore(copied.conn, manifest)
        assert owner.read(copied.conn, new_pid) == {'inputs': mids, 'artists': [artist]}
        assert not copied.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        copied.conn.close()


def test_original_subject_validation_and_ids_survive_retirement(store):
    pid, mids, artist, rid = evidence(store)
    with pytest.raises(ValueError, match='distinct original'), atomic_write(store):
        duplicate = store.conn.execute('INSERT INTO measurements(recording_id,vibe,score,support,source,model_version,input_digest) '
            "SELECT recording_id,vibe,score,support,source,'other',input_digest FROM measurements WHERE id=?", (mids[0],)).lastrowid
        owner.publish_many(store.conn, [(pid, [mids[0], duplicate], [artist])])
    with atomic_write(store):
        for at in (None, 200 * DAY):
            store.conn.execute('INSERT INTO predictions(recording_id,vibe,score,support,kind,policy_version,predicted_at) '
                "VALUES (?,'relax',.5,1,'artist','basis',?)", (rid, at))
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


def test_periodic_preparation_stages_off_lock_and_rejects_stale_allocation(store,monkeypatch):
    _pid,mids,artist,rid = evidence(store)
    key = store.conn.execute('SELECT identity_key FROM recordings WHERE id=?',(rid,)).fetchone()[0]
    tracks = {key:{'evidence':None,'artist':json.dumps([artist]),'genre':None,'version':'basis'}}
    resolved = {key:{'relax':{'source':'artist','score':.5,'support':1.,
                                  'inputs':{'one':mids[0],'two':mids[1]}}}}
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
                store.conn.execute('INSERT INTO predictions(recording_id,vibe,score,support,kind,policy_version,predicted_at) '
                    "VALUES (?,'relax',.5,1,'artist','competing',4)",(rid,))
            assert not snapshot.publish(lambda:store.vibe_predictions.publish_prepared(plan))
    assert calls
    assert not list(owner.directory(store.conn).rglob('.fao-pending-*'))
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),6)) as plan:
            monkeypatch.setattr(fao,'stage_bytes',lambda *a,**k: pytest.fail('Compression under SQL publication'))
            assert snapshot.publish(lambda:store.vibe_predictions.publish_prepared(plan))
    current = store.conn.execute('SELECT prediction_id FROM recording_vibes WHERE recording_id=?',(rid,)).fetchone()[0]
    assert owner.read(store.conn,current)=={'inputs':mids,'artists':[artist]}
    with store.read_snapshot() as snapshot:
        with closing(snapshot.store.vibe_predictions.prepare_publication(tracks,resolved,set(),7)) as plan:
            assert not plan.staged and not plan.predictions
            assert snapshot.publish(lambda:store.vibe_predictions.publish_prepared(plan))
    assert store.conn.execute('SELECT prediction_id FROM recording_vibes WHERE recording_id=?',(rid,)).fetchone()[0]==current
