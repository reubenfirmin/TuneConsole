"""External attributes, normalized score history and current lineage integration."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import sqlite3
import time

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.rec.test_vibe_prediction import (
    capture, controlled_measurements, drain, fits, individual_model_rollout, relax_joint_channel, track,
)
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, without_query_timing,
)
from tests.test_audio_vibes import sample
from yt_playlist.core import catalog, content_work, prepared_inputs, sample_files, profile_files, vibe_provenance, vibe_versions
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_preparation
from yt_playlist.rec import home_vibe
from yt_playlist.rec import vibe_recording_rules
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec.audio_vibe_specialists import SPECIALISTS
from yt_playlist.rec.progressive import Preparation
from yt_playlist.rec.vibe_score_policy import compatible_version


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Repository checks have no web application or periodic sync lifecycle."""
    yield


def test_sample_json_is_external_immutable_and_retains_interrupted_reservations(store, monkeypatch):
    value = sample()
    store.audio_vibes.save_sample(value, 12)
    row = store.conn.execute('SELECT * FROM sample_attributes').fetchone()
    assert sample_files.read(row['filename']) == value
    assert Path(row['filename']).stem == value['id']
    assert row['file_status'] == 'ready'
    assert row['captured_at'] == 12
    assert not {'payload', 'attributes', 'tags', 'traits'} & set(row.keys())
    assert store.audio_vibes.samples(value['run_id']) == [value]
    store.audio_vibes.save_sample(value, 99)
    assert store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 1
    changed = {**value, 'title': 'Conflicting sample'}
    with pytest.raises(ValueError, match='already been saved'):
        store.audio_vibes.save_sample(changed, 100)
    Path(row['filename']).unlink()
    store.audio_vibes.save_sample(value, 101)
    assert sample_files.read(row['filename']) == value
    assert store.conn.execute('SELECT captured_at FROM sample_attributes').fetchone()[0] == 12
    def unavailable(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(store.audio_vibes.sample_files, 'publish_reserved', unavailable)
    fresh = sample()
    with pytest.raises(OSError, match='disk unavailable'):
        store.audio_vibes.save_sample(fresh, 102)
    incomplete = store.conn.execute('SELECT * FROM sample_attributes WHERE id=?', (fresh['id'],)).fetchone()
    assert incomplete['file_status'] == 'pending' and not Path(incomplete['filename']).exists()
    assert store.audio_vibes.sample_receipts(fresh['run_id']) == []


@pytest.fixture
def versioned_store(normalized_vibe_schema, tmp_path, monkeypatch):
    bound_catalog(monkeypatch, entries=(('psybient', 'Psybient'),))
    store = Store(tmp_path / 'vibe-scores.db')
    with closing(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(store, normalized_vibe_schema, snapshot_guards=False)
        with mock_snapshot_coordinator(store, monkeypatch):
            yield store


@pytest.fixture(scope='module')
def normalized_vibe_schema():
    with bounded_consumer_schema((('psybient', 'Psybient'),)) as schema:
        yield schema


def test_versioned_score_input_has_expected_storage_contract(versioned_store):
    conn = versioned_store.conn
    assert vibe_versions.normalized(conn)
    assert 'used_by_model' in {row[1] for row in conn.execute('PRAGMA table_info(measurements)')}
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_score_definitions_reject_unknown_names_and_keep_retired_lineage(versioned_store):
    conn = versioned_store.conn
    definitions = dict(conn.execute('SELECT vibe,kind FROM vibe_definitions'))
    assert {vibe for vibe, kind in definitions.items() if kind == 'specialist'} == set(home_vibe.PRESETS) == {
        vibe for vibe, _ in SPECIALISTS}
    assert {vibe for vibe, kind in definitions.items() if kind == 'intensity'} == set(INTENSITY_CHANNELS)
    assert {vibe for vibe, kind in definitions.items() if kind == 'retired'} == {'shared_energize', 'shared_release'}

    measurement = ('INSERT INTO measurements(id,recording_id,vibe,score,support,source,model_version_id,input_digest)'
                   " VALUES (1,1,?,.5,1,'provider_measurement',"
                   "(SELECT id FROM vibe_versions WHERE value='old-classifier'),'old-input')")
    prediction = ('INSERT INTO vibe_predictions(id,recording_id,vibe,score,support,kind,lineage_complete,policy_version)'
                  " VALUES (1,2,?,.5,1,'artist',0,'old-classifier')")
    current = ("INSERT INTO vibe_recording_fits(recording_id,vibe,score,support,source,inputs_version)"
               " VALUES (1,?,.5,1,'heuristic','old-classifier')")
    with conn:
        vibe_versions.intern(conn, 'old-classifier')
        conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
                         [(1, 'one', 'One', 'Artist'), (2, 'two', 'Two', 'Artist')])
    for insert in (measurement, prediction, current):
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'), conn:
            conn.execute(insert, ('unknown',))
    measured_fit = ("INSERT INTO vibe_recording_fits(recording_id,vibe,score,support,source,measurement_id,inputs_version)"
                    " VALUES (?,?,.5,1,'provider_measurement',1,'old-classifier')")
    predicted_fit = ("INSERT INTO vibe_recording_fits(recording_id,vibe,score,support,source,prediction_id,inputs_version)"
                     " VALUES (?,?,.5,1,'artist',1,'old-classifier')")
    with conn:
        conn.execute(measurement, ('shared_release',))
        conn.execute(prediction, ('shared_energize',))
    for insert, rid, vibe in ((measured_fit, 1, 'shared_energize'), (measured_fit, 2, 'shared_release'),
                              (predicted_fit, 2, 'shared_release'), (predicted_fit, 1, 'shared_energize')):
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'), conn:
            conn.execute(insert, (rid, vibe))
    with conn:
        conn.execute(measured_fit, (1, 'shared_release'))
        conn.execute(predicted_fit, (2, 'shared_energize'))
    assert [tuple(row) for row in conn.execute(
        'SELECT recording_id,vibe,measurement_id,prediction_id FROM recording_vibes ORDER BY recording_id'
    )] == [(1, 'shared_release', 1, None), (2, 'shared_energize', None, 1)]
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.usefixtures('native_sql_calls', 'relax_joint_channel')
def test_current_scores_have_exact_original_inputs_and_no_json_score_copy(versioned_store, monkeypatch):
    store = versioned_store
    track(store, 'original')
    _tid, target = track(store, 'target')
    values = capture(store, 'original')
    drain(store)
    prediction = store.conn.execute("SELECT p.* FROM recording_vibes v JOIN predictions p ON p.id=v.prediction_id "
        "JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()
    assert prediction['lineage_complete'] == 1 and prediction['kind'] == 'artist_genre'
    lineage = vibe_provenance.read(store.conn,prediction['id'])
    inputs = [store.conn.execute('SELECT * FROM measurements WHERE id=?',(mid,)).fetchone() for mid in lineage['inputs']]
    assert len(inputs) == 1 and inputs[0]['source'] == 'audio_measurement'
    assert inputs[0]['vibe'] == 'relax'
    assert {row[0] for row in store.conn.execute('SELECT sample_id FROM measurement_samples WHERE measurement_id=?',
                                                (inputs[0]['id'],))} == {value['id'] for value in values}
    assert lineage['artists']
    assert all('vibes' not in json.loads(profile_files.read(store.conn,'recording',key))
               for key, in store.conn.execute('SELECT identity_key FROM recordings WHERE musical_profile_version IS NOT NULL'))
    before = {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table}'))) for table in (
        'measurements', 'predictions', 'recording_vibes')}
    with store.conn:
        content_work.enqueue(store.conn, (target,))
    drain(store)
    for table, rows in before.items():
        assert list(map(tuple, store.conn.execute(f'SELECT * FROM {table}'))) == rows
    monkeypatch.setattr(vibe_preparation.audio_vibe_specialists, 'identity', lambda: 'new-model')
    drain(store)
    expected_version = vibe_preparation.version()
    assert store.conn.execute('SELECT COUNT(*) FROM measurements').fetchone()[0] > len(before['measurements'])
    assert set(before['measurements']) <= set(map(tuple, store.conn.execute('SELECT * FROM measurements')))
    current = store.conn.execute("SELECT p.* FROM recording_vibes v JOIN predictions p ON p.id=v.prediction_id "
        "JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()
    assert (current['score'], current['support']) == (prediction['score'], prediction['support'])
    assert current['id'] != prediction['id']  # Equal scores do not identify different input versions.
    rid = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (target,)).fetchone()[0]
    assert store.vibe_predictions._current(rid)['version'].startswith(expected_version)
    assert store.vibe_predictions.overlay({target: {}})[target]['vibes']['inputs_version'].startswith(expected_version)
    if vibe_versions.normalized(store.conn):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
            store.conn.execute("UPDATE vibe_versions SET value='changed' WHERE id=1")
        reopened = Store(store._db_path)
        try:
            reopened.conn.execute('PRAGMA synchronous=OFF')
            reopened.init_schema()
            assert reopened.vibe_predictions._current(rid)['version'].startswith(expected_version)
        finally:
            reopened.conn.close()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    plan = ' '.join(str(tuple(row)) for row in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT recording_id,score FROM recording_vibes WHERE vibe='relax' ORDER BY score DESC,recording_id LIMIT 10"))
    assert 'idx_recording_vibe_rank' in plan and 'TEMP B-TREE' not in plan


def test_shared_scores_are_excluded_until_bounded_specialist_refresh(versioned_store, monkeypatch):
    store = versioned_store
    _tid, original = track(store, 'original')
    _tid, target = track(store, 'target')
    samples = capture(store, 'original')
    retired = 'recording-vibes-1/measured-vibes-2/subgenres-only-1/shared-classifier'
    with monkeypatch.context() as patch:
        patch.setattr(vibe_recording_rules, 'version', lambda: retired)
        patch.setattr(vibe_preparation, 'version', lambda: retired)
        drain(store, batch=1)
    before = {row[0] for row in store.conn.execute('SELECT id FROM measurements')}
    receipts = store.audio_vibes.sample_receipts(samples[0]['run_id'])
    assert before
    assert not store.vibe_predictions.fit_scores([original, target], 'relax')
    assert not store.vibe_predictions.ranked('relax')
    embedded = {original: {'dimensions': {'kept': True},
        'vibes': {'inputs_version': retired, 'fits': {'relax': {'score': 1}}}}}
    assert store.vibe_predictions.overlay(embedded) == {original: {'dimensions': {'kept': True}}}
    assert not store.vibe_predictions.joint_inputs('relax', 32)
    group = json.dumps(['artist', '["Shared artist"]', None], separators=(',', ':'))
    assert store.vibe_predictions.groups([group])[group] == {'stats': {}, 'inputs': {}}

    # The ordinary bounded worker replaces current outputs, retaining captures
    # and immutable measurements. There is no migration or eager catalog scan.
    drain(store, batch=1)
    keys = [original, *(f'unknown-{index}' for index in range(512)), target, original]
    with store.read_view() as reader:
        queries = []
        reader.conn.set_trace_callback(queries.append)
        try:
            assert reader.vibe_predictions.fit_scores(keys, 'relax').keys() == {original, target}
        finally:
            reader.conn.set_trace_callback(None)
    [query] = [sql for sql in queries if sql.lstrip().startswith(('SELECT', 'WITH'))]
    assert 'recordings' in query and 'served_vibe_fits' in query
    profiles = store.vibe_predictions.overlay({original: {}, target: {}})
    for profile in profiles.values():
        assert compatible_version(profile['vibes']['inputs_version'])
        assert set(profile['vibes']['intensity_fits']) == {'release', 'energize'}
    assert profiles[original]['vibes']['fits']['relax']['source'] == 'audio_measurement'
    assert profiles[target]['vibes']['fits']['relax']['source'] == 'artist_genre'
    assert before < {row[0] for row in store.conn.execute('SELECT id FROM measurements')}
    assert store.audio_vibes.sample_receipts(samples[0]['run_id']) == receipts
    assert store.vibe_predictions.joint_inputs('relax', 32)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_measurements_reject_wrong_recording_and_changes_to_original_evidence(store):
    _tid, one = track(store, 'one')
    _tid, two = track(store, 'two')
    capture(store, 'one')
    capture(store, 'two', calm=False)
    drain(store)
    own = store.conn.execute("SELECT m.* FROM measurements m JOIN recordings r ON r.id=m.recording_id "
                             "WHERE r.identity_key=? AND m.vibe='relax'", (one,)).fetchone()
    other_sample = store.conn.execute("SELECT s.id FROM sample_attributes s JOIN recordings r ON r.id=s.recording_id "
                                      'WHERE r.identity_key=? LIMIT 1', (two,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='another recording'), store.conn:
        store.conn.execute('INSERT INTO measurement_samples VALUES (?,?)', (own['id'], other_sample))
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
        store.conn.execute("UPDATE measurements SET vibe='release' WHERE id=?", (own['id'],))
    with pytest.raises(sqlite3.IntegrityError, match='Retire measurement'), store.conn:
        store.conn.execute('UPDATE sample_attributes SET recording_id=? WHERE id=?', (own['recording_id'], other_sample))


def test_missing_or_misidentified_files_fall_back_and_can_be_recovered(store):
    _tid, original = track(store, 'original')
    _tid, target = track(store, 'target')
    first = capture(store, 'original')
    drain(store)
    before = fits(store, target)
    newer = capture(store, 'original', calm=False)
    row = store.conn.execute('SELECT filename FROM sample_attributes WHERE id=?', (newer[0]['id'],)).fetchone()
    Path(row[0]).unlink()
    drain(store)
    assert fits(store, target) == before
    # A valid content-addressed file from a different excerpt is still not this sample.
    wrong = store.conn.execute('SELECT filename FROM sample_attributes WHERE id=?', (newer[1]['id'],)).fetchone()[0]
    with store.conn:
        store.conn.execute('UPDATE sample_attributes SET filename=? WHERE id=?', (wrong, first[0]['id']))
    drain(store)
    assert fits(store, original)['relax']['support'] == pytest.approx(2. / 3.)
    assert 0. < fits(store, target)['relax']['support'] < before['relax']['support']
    admitted = store.conn.execute(
        "SELECT sample_id FROM measurement_samples s JOIN recording_vibes v ON v.measurement_id=s.measurement_id "
        "JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (original,))
    assert {row[0] for row in admitted} == {row['id'] for row in newer[1:]}
    assert len(store.audio_vibes.samples(first[0]['run_id'])) == 2


def test_exact_video_reassociation_retracts_old_vote_and_preserves_history(store):
    _tid, original = track(store, 'original')
    tid, canonical = track(store, 'canonical')
    _tid, target = track(store, 'target')
    capture(store, 'original')
    capture(store, 'canonical', calm=False)
    drain(store)
    old = list(map(tuple, store.conn.execute('SELECT * FROM measurements')))
    rid = store.conn.execute('SELECT recording_id FROM track_sources WHERE id=?', (tid,)).fetchone()[0]
    with store.conn:
        catalog.associate_item(store.conn, 'original', rid, source='verified', evidence={'same': True}, now=300, same_recording=True)
    assert store.conn.execute("SELECT recording_id FROM sample_attributes WHERE video_id='original'").fetchone()[0] == rid
    drain(store, batch=1)
    assert fits(store, target)['relax']['recordings'] == 1
    assert set(old) <= set(map(tuple, store.conn.execute('SELECT * FROM measurements')))
    assert not store.conn.execute('SELECT 1 FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id '
                                  'WHERE r.identity_key=?', (original,)).fetchone()
    assert fits(store, canonical)['relax']['source'] == 'audio_measurement'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture
def dense_cohort_store(tmp_path, monkeypatch):
    # This owner measures real cohort SQL, inference and exact persisted
    # lineage. Catalog curation and concurrent snapshot admission are below
    # its boundary; retain the full 2,000 donors and all relational guards.
    from yt_playlist.core.db_diagnostics import DiagnosticCursor
    from yt_playlist.util import genre_catalog

    # Keep native SQLite iteration beneath the repository; per-row diagnostic
    # clocks and handle admission do not establish cohort or lineage behavior.
    monkeypatch.setattr(DiagnosticCursor, '__next__', sqlite3.Cursor.__next__)
    bound_catalog(monkeypatch, entries=(('psybient', 'Psybient'),))
    stamp = vibe_preparation.serving_prefix() + 'completed-cohort-models'
    monkeypatch.setattr(vibe_preparation, 'version', lambda: stamp)
    monkeypatch.setattr(vibe_recording_rules, 'version', lambda: stamp)
    with bounded_consumer_schema((('psybient', 'Psybient'),), snapshot_guards=False) as schema:
        store = Store(tmp_path / 'dense-cohort.db')
        try:
            store.conn.execute('PRAGMA synchronous=OFF')
            initialize_consumer(store, schema, snapshot_guards=False)
            # The bounded vocabulary has no ancestry edges. Do not decode the
            # unrelated shipped graph beneath the cohort calculation.
            monkeypatch.setattr(genre_catalog, '_parents', lambda: {})
            store.conn.execute('PRAGMA wal_autocheckpoint=0')
            with mock_snapshot_coordinator(store, monkeypatch):
                yield store
        finally:
            store.conn.close()


def test_dense_cohort_finishes_one_recording_with_exact_inputs(dense_cohort_store, relax_joint_channel):
    """A common cohort must not permanently exceed a per-page row ceiling."""
    store = dense_cohort_store
    _tid, target = track(store, 'target')
    artist = store.vibe_predictions.inputs([target])[target]['artist']
    stamp = vibe_preparation.version()
    # Controlled density fixture: 2,000 provider-measured recordings, six vibes
    # each, shared by all three cohorts. No synthetic raw audio or human labels.
    # The worker captures the target normally. Donors are controlled accepted
    # inputs and original measurements, not an ingestion/provider-selection test.
    # Keep all 2,000 real SQL subjects without repeating metadata selection and
    # artist observation for each of the already-defined cohort contributors.
    first = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM recordings').fetchone()[0]
    first_entry = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM track_sources').fetchone()[0]
    donors = [(first+i, f'dense-{i}', f'Dense {i}') for i in range(2000)]
    donor_inputs = {'library': {'genre': 'Psybient', 'year': None, 'audio': {}}, 'discovered': None}
    donor_digest = hashlib.sha256(json.dumps(donor_inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    accepted = (*prepared_inputs.flatten(donor_inputs), 1, donor_digest, 1)
    with store.conn:
        store.conn.execute('UPDATE rec_vibe_pending SET rebuild_policy=? WHERE position=0',
                           (vibe_recording_rules.rebuild_version(),))
        store.conn.executemany("INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,'Shared artist')", donors)
        end = first + len(donors)
        store.conn.execute('INSERT INTO track_sources(id,recording_id,video_id,title,artist) '
            'SELECT ?+id-?,id,identity_key,title,artist FROM recordings WHERE id>=? AND id<?',
            (first_entry, first, first, end))
        store.conn.execute("UPDATE track_sources SET library_origin='fixture' "
            'WHERE recording_id>=? AND recording_id<?', (first, end))
        columns = ','.join(prepared_inputs.COLUMNS)
        store.conn.execute(f'INSERT INTO prepared_recording_inputs(recording_id,{columns}) SELECT id,'
            + ','.join('?' for _ in accepted) + ' FROM recordings WHERE id>=? AND id<?',
            (*accepted, first, end))
        store.conn.execute("INSERT OR IGNORE INTO genre_catalog(key,name,source) VALUES ('psybient','Psybient','custom')")
        version = vibe_versions.column(store.conn, 'measurements')
        stored_version = vibe_versions.stored(store.conn, stamp)
        first_measurement = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM measurements').fetchone()[0]
        # Set-based fixture insertion keeps all 12,000 independently guarded
        # measurements without crossing the DB wrapper once per accepted row.
        store.conn.execute('WITH vibes(ordinal,vibe) AS (VALUES '
            + ','.join(f'({i},?)' for i in range(len(home_vibe.PRESETS))) + ') '
            f'INSERT INTO measurements(id,recording_id,vibe,score,support,source,{version},input_digest,measured_at) '
            "SELECT ?+(r.id-?)*?+v.ordinal,r.id,v.vibe,.8,1,'audio_measurement',?,'density',1 "
            'FROM recordings r CROSS JOIN vibes v WHERE r.id>=? AND r.id<?',
            (*home_vibe.PRESETS, first_measurement, first, len(home_vibe.PRESETS), stored_version, first, end))
        store.conn.execute('INSERT INTO recording_vibes '
            f"SELECT recording_id,vibe,score,support,source,id,NULL,{version},?,'psybient',NULL,NULL "
            'FROM measurements WHERE recording_id>=? AND recording_id<?', (artist, first, end))
        store.conn.execute('UPDATE rec_musical_sources SET pending_position=NULL,pending_cursor=NULL')
        store.conn.execute('DELETE FROM rec_vibe_pending WHERE position>0')
        content_work.acknowledge(store.conn, content_work.pending(store.conn, -1))
        content_work.enqueue(store.conn, (target,))
    # This bulk fixture has a dense artist cohort. Let SQLite select its
    # existing ordered index from the actual distribution, rather than sorting
    # all 12,000 scores again for each 128-row page. Statistics cost is timed too.
    store.conn.execute('ANALYZE recording_vibes')
    assert not store.prepared_content.missing_unqueued(1)
    from yt_playlist.rec import vibe_joint_preparation

    assert vibe_joint_preparation.step(store)
    started = time.monotonic()
    assert Preparation(store).capture(1000)
    elapsed = time.monotonic() - started
    assert fits(store, target)['relax']['recordings'] == 2000
    assert elapsed < 3., f'Dense one-recording scoring took {elapsed:.3f}s'
    pid = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id "
                             "WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    assert len(vibe_provenance.read(store.conn,pid)['inputs']) == 2000


def test_cohort_fanout_keeps_partial_evidence_and_model_refresh_visits_every_recording(dense_cohort_store):
    from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
    from yt_playlist.rec import vibe_prediction

    store = dense_cohort_store
    keys = ('a-measured', 'b-partial', 'c-fallback', 'd-unrelated', 'e-unprepared')
    channels = (*home_vibe.PRESETS, *INTENSITY_CHANNELS)
    stamp = vibe_preparation.version()
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
                               [(index, key, key, 'Shared') for index, key in enumerate(keys, 1)])
        version = vibe_versions.column(store.conn, 'measurements')
        stored_version = vibe_versions.stored(store.conn, stamp)
        originals = [(index, 1, channel) for index, channel in enumerate(channels, 1)]
        originals.append((len(originals) + 1, 2, 'relax'))
        store.conn.executemany(f'INSERT INTO measurements(id,recording_id,vibe,score,support,source,{version},'
            "input_digest,measured_at) VALUES (?,?,?,.8,1,'provider_measurement',?,'fixture',1)",
            [(*row, stored_version) for row in originals])
        store.conn.executemany('INSERT INTO recording_vibes VALUES (?,?,.8,1,\'provider_measurement\',?,NULL,?, '
            '\'[1]\',\'psybient\',NULL,NULL)',
            [(rid, channel, mid, stored_version) for mid, rid, channel in originals])
        store.conn.executemany('INSERT INTO recording_vibes VALUES (?,?,.5,0,\'heuristic\',NULL,NULL,?, '
            '?,?,NULL,NULL)', [(2, 'energize', stored_version, '[1]', 'psybient'),
                              (3, 'relax', stored_version, '[1]', 'psybient'),
                              (4, 'relax', stored_version, '[2]', None)])
        content_work.acknowledge(store.conn, content_work.pending(store.conn, -1))
        store.conn.execute('DELETE FROM rec_vibe_pending WHERE position>0')

    def drain_pages():
        for _ in range(2 * len(keys) + 3):
            if not store.vibe_predictions.pending():
                return
            assert store.vibe_predictions.fanout(1)
        pytest.fail('Single-recording fanout pages did not finish')

    # Each dependency kind must retain partially measured and unmeasured
    # recipients, without reprocessing independent measured recordings.
    for group in vibe_prediction.groups('[1]', 'psybient'):
        store.vibe_predictions.repository.queue([group])
        drain_pages()
        assert {key for key, _ in content_work.pending(store.conn, 10)} == {
            'b-partial', 'c-fallback'}
        with store.conn:
            content_work.acknowledge(store.conn, content_work.pending(store.conn, -1))
    store.vibe_predictions.ensure_version(stamp)
    drain_pages()
    assert {key for key, _ in content_work.pending(store.conn, 10)} == set(keys)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_model_refreshes_coalesce_without_rewinding_an_active_sweep(dense_cohort_store):
    store = dense_cohort_store
    repo = store.vibe_predictions.repository
    keys = ('a', 'b', 'c')
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(identity_key,title,artist) VALUES (?,?,?)',
                               [(key, key, 'Shared') for key in keys])
        content_work.acknowledge(store.conn, content_work.pending(store.conn, -1))
    repo.queue(['*'])
    for cursor in keys:
        assert repo.fanout(1)
        assert repo.pending()['cursor'] == cursor
        repo.queue(['*'])
        assert repo.pending()['cursor'] == cursor
    first_sweep = dict(content_work.pending(store.conn, 10))
    assert store.conn.execute("SELECT count(*) FROM rec_vibe_pending WHERE group_key='*next'").fetchone()[0] == 1
    assert repo.fanout(1)
    assert repo.pending()['cursor'] == ''
    for cursor in keys:
        assert repo.fanout(1)
        assert repo.pending()['cursor'] == cursor
    assert repo.fanout(1)
    assert repo.pending() is None
    refreshed = content_work.pending(store.conn, 10)
    assert [key for key, _ in refreshed] == list(keys)
    assert all(revision > first_sweep[key] for key, revision in refreshed)


@pytest.mark.parametrize('scope', prepared_inputs.SCOPES)
def test_refresh_writes_prepared_inputs_without_schema_probes(versioned_store, scope):
    store = versioned_store
    _entry, recording = track(store, 'prepared-rollover')
    keys = recording, 'unresolved|artist'
    payload = dict.fromkeys(prepared_inputs.SCOPES)
    payload[scope] = {'genre': 'Psybient', 'year': None, 'audio': {'bpm': 118, 'mood_relaxed': .6}}
    with store.conn:
        content_work.acknowledge(store.conn, content_work.pending(store.conn, -1))
        for key in keys:
            prepared_inputs.upsert(store.conn, key, payload, '0' * 64, 1, 1)

    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        for turn in (2, 3):
            payload[scope]['audio']['bpm'] += 1
            with store.conn:
                content_work.enqueue(store.conn, keys, bump=False)
            pending = store.prepared_content.pending()
            assert {key for key, _rev in pending} == set(keys)
            assert store.prepared_content.publish_capture(
                [(key, rev, json.dumps(payload), str(turn) * 64, True) for key, rev in pending], turn) == 2
            assert not store.prepared_content.has_pending()
            accepted = store.conn.execute('SELECT * FROM prepared_recording_inputs').fetchall()
            assert len(accepted) == 2
            for row in accepted:
                assert prepared_inputs.payload(row) == payload
                assert row[f'{scope}_audio_fields'] == (prepared_inputs.AUDIO_BITS['bpm']
                                                         | prepared_inputs.AUDIO_BITS['mood_relaxed'])
                assert all(f'{cohort}_energy' not in row.keys() for cohort in prepared_inputs.SCOPES)
    finally:
        store.conn.set_trace_callback(None)
    assert not [sql for sql in statements if any(probe in sql.lower() for probe in
                ('table_info', 'table_xinfo', 'sqlite_schema', 'sqlite_master'))]
