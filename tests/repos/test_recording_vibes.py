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
from tests.rec.test_vibe_prediction import capture, drain, fits, track
from tests.test_audio_vibes import sample
from yt_playlist.core import catalog, prepared_inputs, sample_files, profile_files, vibe_provenance, vibe_versions
from yt_playlist.core.db_rebuild import build, destination
from yt_playlist.core.migrations import MigrationError, current, migrate
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_preparation
from yt_playlist.rec import home_vibe
from yt_playlist.rec.progressive import Preparation


def test_sample_json_is_external_immutable_and_durable_before_reference(store, monkeypatch):
    value = sample()
    store.audio_vibes.save_sample(value, 12)
    row = store.conn.execute('SELECT * FROM sample_attributes').fetchone()
    assert sample_files.read(row['filename']) == value
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
    monkeypatch.setattr(sample_files, 'publish', unavailable)
    fresh = sample()
    with pytest.raises(OSError, match='disk unavailable'):
        store.audio_vibes.save_sample(fresh, 102)
    assert store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 1


@pytest.fixture(scope='module')
def installed_v107_template(tmp_path_factory):
    """The actual earlier fresh format, followed by its bounded admission."""
    with closing(sqlite3.connect(':memory:')) as template:
        template.row_factory = sqlite3.Row
        build._configure(template)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('installed-v107')))
            template.execute('BEGIN IMMEDIATE')
            current.bootstrap(template, MIGRATIONS, 0.)
            template.commit()
            migrate(template)
        assert not vibe_versions.normalized(template)
        template.execute('PRAGMA query_only=ON')
        before = list(template.iterdump())
        yield template
        assert list(template.iterdump()) == before


@pytest.fixture(params=('installed', 'normalized'))
def versioned_store(request, tmp_path):
    if request.param == 'installed':
        template = request.getfixturevalue('installed_v107_template')
        installed = Store(':memory:')
        try:
            template.backup(installed.conn)
            installed.init_schema()
            assert not vibe_versions.normalized(installed.conn)
            yield installed
        finally:
            installed.conn.close()
        return
    path = tmp_path/'normalized.db'
    with sqlite3.connect(path) as conn:
        build._configure(conn)
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
    try:
        normalized.init_schema()
        assert vibe_versions.normalized(normalized.conn)
        yield normalized
    finally:
        normalized.conn.close()


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
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'new-model')
    drain(store)
    assert store.conn.execute('SELECT COUNT(*) FROM measurements').fetchone()[0] > len(before['measurements'])
    assert set(before['measurements']) <= set(map(tuple, store.conn.execute('SELECT * FROM measurements')))
    current = store.conn.execute("SELECT p.* FROM recording_vibes v JOIN predictions p ON p.id=v.prediction_id "
        "JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()
    assert (current['score'], current['support']) == (prediction['score'], prediction['support'])
    assert current['id'] != prediction['id']  # Equal scores do not identify different input versions.
    rid = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (target,)).fetchone()[0]
    assert store.vibe_predictions._current(rid)['version'] == vibe_preparation.version()
    assert store.vibe_predictions.overlay({target: {}})[target]['vibes']['inputs_version'] == vibe_preparation.version()
    if vibe_versions.normalized(store.conn):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
            store.conn.execute("UPDATE vibe_versions SET value='changed' WHERE id=1")
        reopened = Store(store._db_path)
        try:
            reopened.init_schema()
            assert reopened.vibe_predictions._current(rid)['version'] == vibe_preparation.version()
        finally:
            reopened.conn.close()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    plan = ' '.join(str(tuple(row)) for row in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT recording_id,score FROM recording_vibes WHERE vibe='relax' ORDER BY score DESC,recording_id LIMIT 10"))
    assert 'idx_recording_vibe_rank' in plan and 'TEMP B-TREE' not in plan


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


def legacy(path):
    store = Store(path)
    try:
        migrate(store.conn, registry=MIGRATIONS[:60])
        with store.conn:
            # Populate the frozen predecessor directly; current catalog writers
            # require the current mood and identity relations.
            key = 'known|artist'
            rid = store.conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) '
                                     "VALUES (?,'Known','Artist',1)", (key,)).lastrowid
            store.conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist) "
                               "VALUES ('known',?,'Known','Artist')", (rid,))
            store.conn.execute("INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,'known')", (rid,))
            value = sample()
            value['video_id'] = 'known'
            store.conn.execute('INSERT INTO audio_vibe_samples VALUES (?,?,?,?,?)',
                               (value['id'], value['run_id'], value['point'], json.dumps(value), 10))
            evidence = {'source': 'audio_measurement', 'sample_ids': [value['id']], 'fits': {'relax': {'score': .8, 'support': 1.}}}
            store.conn.execute('INSERT INTO rec_vibe_tracks VALUES (?,?,?,?,?)', (key, None, 'psybient', json.dumps(evidence), 'old-model'))
            profile = {'vibes': {'fits': {'relax': {'score': .8, 'support': 1., 'source': 'audio_measurement'}}}}
            store.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)', (key, json.dumps(profile), 'old', 'digest', 20))
            store.conn.execute('INSERT INTO audio_session_labels VALUES (?,?,?,?,?,?)',
                               (value['run_id'], 'retained-taxonomy', json.dumps([value['id']]), 'null', '["relax"]', 11))
        return store, value
    except BaseException:
        store.conn.close()
        raise


@pytest.fixture(scope='module')
def recording_vibe_predecessor():
    """Retain a genuine populated v60 prefix; each upgrade owns private files."""
    with ExitStack() as resources:
        directory = Path(resources.enter_context(TemporaryDirectory(prefix='recording-vibe-v60-')))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(directory / 'app-home'))
            patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
            patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
            patch.delenv('DEEPSEEK_API_KEY', raising=False)
            source, value = legacy(directory / 'source.db')
            resources.callback(source.conn.close)
            template = capture_template(source, value)
        yield template


def test_populated_upgrade_preserves_samples_scores_labels_and_reopens(tmp_path, recording_vibe_predecessor):
    store = Store(tmp_path / 'legacy.db')
    value = recording_vibe_predecessor.restore(store)
    labels = store.conn.execute('SELECT * FROM audio_session_labels').fetchone()
    labels = {key: labels[key] for key in labels.keys()}
    migrate(store.conn,registry=MIGRATIONS)
    store.init_schema()
    assert store.audio_vibes.samples(value['run_id']) == [value]
    retained = store.audio_vibes.earlier_feedback(value['run_id'])[0]
    assert retained == {'taxonomy_version': labels['taxonomy_version'], 'vibes': json.loads(labels['vibes']),
                        'sample_ids': json.loads(labels['sample_ids']), 'prediction': json.loads(labels['prediction']),
                        'updated_at': labels['updated_at']}
    score = store.conn.execute('SELECT * FROM measurements').fetchone()
    assert (score['score'], score['model_version'], score['measured_at']) == (.8, 'old-model', None)
    assert store.conn.execute('SELECT measurement_id FROM recording_vibes').fetchone()[0] == score['id']
    assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('audio_vibe_samples','rec_vibe_tracks','rec_vibe_groups')").fetchone()
    assert not (tmp_path / 'backups').exists()
    store.conn.close()
    reopened = Store(tmp_path / 'legacy.db')
    try:
        reopened.init_schema()
        assert reopened.audio_vibes.samples(value['run_id']) == [value]
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


def test_externalization_failure_rolls_back_database_and_can_resume(tmp_path, monkeypatch, recording_vibe_predecessor):
    store = Store(tmp_path / 'legacy.db')
    value = recording_vibe_predecessor.restore(store)
    before = list(store.conn.iterdump())
    original = sample_files.publish
    def fail_after_publication(*args):
        original(*args)
        raise OSError('interrupted externalization')
    with monkeypatch.context() as patch:
        patch.setattr(sample_files, 'publish', fail_after_publication)
        with pytest.raises(MigrationError, match='interrupted externalization'):
            migrate(store.conn,registry=MIGRATIONS[:61])
    assert list(store.conn.iterdump()) == before
    # This failure belongs to v61. The populated upgrade/reopen case above
    # owns the complete suffix and current application admission.
    migrate(store.conn,registry=MIGRATIONS[:61])
    validate_version(store.conn, 61)
    store._bind_repositories()
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 61
    assert store.audio_vibes.samples(value['run_id']) == [value]
    assert len(list((tmp_path / 'sample-attributes').glob('*/*.json'))) == 1
    store.conn.close()


def test_dense_cohort_finishes_one_recording_with_exact_inputs(store):
    """A common cohort must not permanently exceed a per-page row ceiling."""
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
    assert not store.prepared_content.missing_unqueued(1)
    started = time.monotonic()
    assert Preparation(store).capture(1000)
    elapsed = time.monotonic() - started
    assert fits(store, target)['relax']['recordings'] == 2000
    assert elapsed < 3., f'Dense one-recording preparation took {elapsed:.3f}s'
    pid = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id "
                             "WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    assert len(vibe_provenance.read(store.conn,pid)['inputs']) == 2000
