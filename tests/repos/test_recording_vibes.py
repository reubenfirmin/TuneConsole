"""External attributes, normalized score history and current lineage integration."""
from pathlib import Path
from contextlib import ExitStack, closing
import hashlib
import json
import sqlite3
import time
from tempfile import TemporaryDirectory

import pytest

from tests.prepared_fixture_support import capture_template
from tests.genre_catalog_support import bound_catalog
from tests.rec.test_vibe_prediction import (
    capture, controlled_measurements, drain, fits, individual_model_rollout, track,
)
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer,
    install_numbered_input, mock_snapshot_coordinator, native_sql_calls, without_query_timing,
)
from tests.test_audio_vibes import sample
from yt_playlist.core import catalog, prepared_inputs, sample_files, profile_files, vibe_provenance, vibe_versions
from yt_playlist.core.db_rebuild import build, destination
from yt_playlist.core.migrations import MigrationError, migrate, regular
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_preparation
from yt_playlist.rec import home_vibe
from yt_playlist.rec import vibe_recording_rules
from yt_playlist.core.audio_vibe_contract import serving_compatible
from yt_playlist.rec.progressive import Preparation


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


@pytest.fixture(scope='module')
def installed_v107_template(tmp_path_factory):
    """The actual earlier fresh format, followed by its bounded admission."""
    with closing(sqlite3.connect(':memory:')) as template:
        template.row_factory = sqlite3.Row
        build._configure(template)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('installed-v107')))
            bound_catalog(patch, entries=(('psybient', 'Psybient'),))
            # The repository owner consumes this exact installed contract.
            # Full bootstrap/admission have their independent current-schema
            # owners; avoid rerunning them beneath score provenance assertions.
            install_numbered_input(template, len(MIGRATIONS), regular_owners=regular.CHANGES)
        assert not vibe_versions.normalized(template)
        template.execute('PRAGMA query_only=ON')
        before = list(template.iterdump())
        yield template
        assert list(template.iterdump()) == before


def test_installed_vibe_schema_input_is_immutable_and_keeps_original_versions(installed_v107_template):
    assert installed_v107_template.execute('PRAGMA query_only').fetchone()[0] == 1
    assert not vibe_versions.normalized(installed_v107_template)
    assert not installed_v107_template.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(params=('installed', 'normalized'))
def versioned_store(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch, entries=(('psybient', 'Psybient'),))
    if request.param == 'installed':
        template = request.getfixturevalue('installed_v107_template')
        installed = Store(tmp_path / 'installed-scores.db')
        try:
            installed.conn.execute('PRAGMA synchronous=OFF')
            initialize_consumer(installed, template, snapshot_guards=False)
            assert not vibe_versions.normalized(installed.conn)
            # Provenance owns committed score inputs and versions. Real SQL and
            # files sit below an uncontended snapshot coordinator in this case.
            with mock_snapshot_coordinator(installed, monkeypatch):
                yield installed
        finally:
            installed.conn.close()
        return
    template = request.getfixturevalue('normalized_vibe_schema')
    normalized = Store(tmp_path / 'normalized.db')
    try:
        normalized.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(normalized, template, snapshot_guards=False)
        assert vibe_versions.normalized(normalized.conn)
        with mock_snapshot_coordinator(normalized, monkeypatch):
            yield normalized
    finally:
        normalized.conn.close()


@pytest.fixture(scope='module')
def normalized_vibe_schema(tmp_path_factory):
    # Score provenance consumes an admitted empty native layout. Build it once
    # through the real initializer and startup; each consumer gets private SQL.
    path = tmp_path_factory.mktemp('normalized-vibe-schema') / 'template.db'
    with closing(sqlite3.connect(path)) as conn, conn:
        build._configure(conn)
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('BEGIN IMMEDIATE')
        destination.initialize(conn)
        cursor = None
        while True:
            batch = destination.indexes(conn, conn, cursor, 10000)
            if batch.done:
                break
            cursor = batch.cursor
        assert destination.finish(conn, conn, None, 10000).done
    normalized = Store(path)
    with closing(normalized.conn):
        # A module fixture can be requested after a function-scoped vocabulary
        # patch. Restore construction patches before yielding, so their later
        # teardown cannot resurrect that already-finished consumer's bounds.
        with pytest.MonkeyPatch.context() as patch:
            bound_catalog(patch, entries=(('psybient', 'Psybient'),))
            normalized.conn.execute('PRAGMA synchronous=OFF')
            normalized.init_schema()
        assert vibe_versions.normalized(normalized.conn)
        normalized.conn.execute('PRAGMA query_only=ON')
        yield normalized.conn
        assert normalized.conn.execute('PRAGMA query_only').fetchone()[0] == 1


def test_versioned_score_input_has_expected_storage_contract(versioned_store, request):
    assert vibe_versions.normalized(versioned_store.conn) == (request.node.callspec.params['versioned_store'] == 'normalized')
    assert not versioned_store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.usefixtures('native_sql_calls', 'individual_model_rollout')
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
               for key, in store.conn.execute('SELECT identity_key FROM rec_musical_profiles'))
    before = {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table}'))) for table in (
        'measurements', 'predictions', 'prediction_inputs', 'recording_vibes')}
    with store.conn:
        store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?)', (target,))
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
    assert store.vibe_predictions._current(rid)['version'] == expected_version
    assert store.vibe_predictions.overlay({target: {}})[target]['vibes']['inputs_version'] == expected_version
    if vibe_versions.normalized(store.conn):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
            store.conn.execute("UPDATE vibe_versions SET value='changed' WHERE id=1")
        reopened = Store(store._db_path)
        try:
            reopened.conn.execute('PRAGMA synchronous=OFF')
            reopened.init_schema()
            assert reopened.vibe_predictions._current(rid)['version'] == expected_version
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
    assert store.vibe_predictions.fit_scores([original, target], 'relax').keys() == {original, target}
    profiles = store.vibe_predictions.overlay({original: {}, target: {}})
    for profile in profiles.values():
        assert serving_compatible(profile['vibes']['inputs_version'])
        assert set(profile['vibes']['intensity_fits']) == {'release', 'energize'}
    assert profiles[original]['vibes']['fits']['relax']['source'] == 'audio_measurement'
    assert profiles[target]['vibes']['fits']['relax']['source'] == 'artist_genre'
    assert before < {row[0] for row in store.conn.execute('SELECT id FROM measurements')}
    assert store.audio_vibes.sample_receipts(samples[0]['run_id']) == receipts
    assert store.vibe_predictions.joint_inputs('relax', 32)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_relations_reject_wrong_recording_wrong_vibe_and_duplicate_source_votes(store):
    _tid, one = track(store, 'one')
    _tid, two = track(store, 'two')
    capture(store, 'one')
    capture(store, 'two', calm=False)
    _tid, target = track(store, 'target')
    drain(store)
    own = store.conn.execute("SELECT m.* FROM measurements m JOIN recordings r ON r.id=m.recording_id "
                             "WHERE r.identity_key=? AND m.vibe='relax'", (one,)).fetchone()
    other_sample = store.conn.execute("SELECT s.id FROM sample_attributes s JOIN recordings r ON r.id=s.recording_id "
                                      'WHERE r.identity_key=? LIMIT 1', (two,)).fetchone()[0]
    pid = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id "
                             "WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='another recording'), store.conn:
        store.conn.execute('INSERT INTO measurement_samples VALUES (?,?)', (own['id'], other_sample))
    wrong = store.conn.execute("SELECT id FROM measurements WHERE vibe='release' LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='distinct original'), store.conn:
        store.conn.execute('INSERT INTO prediction_inputs VALUES (?,?)', (pid, wrong))
    with store.conn:
        version = vibe_versions.column(store.conn, 'measurements')
        duplicate = store.conn.execute(f'INSERT INTO measurements(recording_id,vibe,score,support,source,{version},input_digest,measured_at) '
            'VALUES (?,?,?,?,?,?,?,?)', (own['recording_id'], 'relax', .5, 1., 'provider_measurement',
                                       vibe_versions.stored(store.conn, 'other'), 'other', 1)).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match='distinct original'), store.conn:
        store.conn.execute('INSERT INTO prediction_inputs VALUES (?,?)', (pid, duplicate))
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
        store.conn.execute("UPDATE measurements SET vibe='release' WHERE id=?", (own['id'],))
    with pytest.raises(sqlite3.IntegrityError, match='Retire measurement'), store.conn:
        store.conn.execute('UPDATE sample_attributes SET recording_id=? WHERE id=?', (own['recording_id'], other_sample))


def test_missing_or_misidentified_files_fall_back_and_can_be_recovered(store):
    track(store, 'original')
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
    assert fits(store, target)['relax']['source'] == 'heuristic'
    assert len(store.audio_vibes.samples(first[0]['run_id'])) == 2


def test_exact_video_reassociation_retracts_old_vote_and_preserves_history(store):
    _tid, original = track(store, 'original')
    tid, canonical = track(store, 'canonical')
    _tid, target = track(store, 'target')
    capture(store, 'original')
    capture(store, 'canonical', calm=False)
    drain(store)
    old = list(map(tuple, store.conn.execute('SELECT * FROM measurements')))
    rid = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (tid,)).fetchone()[0]
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


def test_dense_cohort_finishes_one_recording_with_exact_inputs(dense_cohort_store):
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
    first_entry = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM catalog_entries').fetchone()[0]
    donors = [(first+i, f'dense-{i}', f'Dense {i}') for i in range(2000)]
    donor_inputs = {'library': {'genre': 'Psybient', 'year': None, 'audio': {}}, 'discovered': None}
    donor_digest = hashlib.sha256(json.dumps(donor_inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    accepted = (*prepared_inputs.flatten(donor_inputs), 1, donor_digest, 1)
    store.settings.publish_values({'rec_vibe_version': stamp})
    with store.conn:
        store.conn.executemany("INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,'Shared artist')", donors)
        end = first + len(donors)
        store.conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist) "
            "SELECT identity_key,id,title,artist FROM recordings WHERE id>=? AND id<?", (first, end))
        store.conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id) '
            'SELECT ?+id-?,id,identity_key FROM recordings WHERE id>=? AND id<?',
            (first_entry, first, first, end))
        store.conn.execute("INSERT INTO library_memberships SELECT id,'fixture',NULL FROM catalog_entries "
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
            "SELECT ?+(r.id-?)*?+v.ordinal,r.id,v.vibe,.8,1,'provider_measurement',?,'density',1 "
            'FROM recordings r CROSS JOIN vibes v WHERE r.id>=? AND r.id<?',
            (*home_vibe.PRESETS, first_measurement, first, len(home_vibe.PRESETS), stored_version, first, end))
        store.conn.execute('INSERT INTO recording_vibes '
            f"SELECT recording_id,vibe,score,support,source,id,NULL,{version},?,'psybient',NULL,NULL "
            'FROM measurements WHERE recording_id>=? AND recording_id<?', (artist, first, end))
        store.conn.execute('DELETE FROM rec_musical_pending')
        store.conn.execute('DELETE FROM rec_vibe_pending')
        store.conn.execute('DELETE FROM rec_content_pending')
        store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?)', (target,))
    # This bulk fixture has a dense artist cohort. Let SQLite select its
    # existing ordered index from the actual distribution, rather than sorting
    # all 12,000 scores again for each 128-row page. Statistics cost is timed too.
    store.conn.execute('ANALYZE recording_vibes')
    assert not store.prepared_content.missing_unqueued(1)
    started = time.monotonic()
    assert Preparation(store).capture(1000)
    elapsed = time.monotonic() - started
    assert fits(store, target)['relax']['recordings'] == 2000
    assert elapsed < 3., f'Dense one-recording preparation took {elapsed:.3f}s'
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
        store.conn.execute('DELETE FROM rec_content_pending')
        store.conn.execute('DELETE FROM rec_vibe_pending')

    def drain_pages():
        for _ in range(len(keys) + 2):
            if not store.vibe_predictions.pending():
                return
            assert store.vibe_predictions.fanout(1)
        pytest.fail('Single-recording fanout pages did not finish')

    # Each dependency kind must retain partially measured and unmeasured
    # recipients, without reprocessing independent measured recordings.
    for group in vibe_prediction.groups('[1]', 'psybient'):
        store.vibe_predictions.repository.queue([group])
        drain_pages()
        assert {row[0] for row in store.conn.execute('SELECT identity_key FROM rec_content_pending')} == {
            'b-partial', 'c-fallback'}
        with store.conn:
            store.conn.execute('DELETE FROM rec_content_pending')
    store.vibe_predictions.ensure_version(stamp)
    drain_pages()
    assert {row[0] for row in store.conn.execute('SELECT identity_key FROM rec_content_pending')} == set(keys)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
