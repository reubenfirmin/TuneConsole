"""Controlled actual-library shared-generation and independent restore rehearsal.

Call through test-library.py. This deliberately fits the captured warm corpus
directly; it does not establish ordinary scheduling, startup or qualification.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import tempfile
import time
import zipfile

from tests.file_custody_support import export_owner, restore_owner
from yt_playlist.core import fao, vibe_provenance
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_joint_model as model, vibe_joint_preparation as preparation, vibe_preparation
from yt_playlist.repos import vibe_models
from .vibe_pooling_operations import storage


def run(output):
    database = Path(os.environ.get('TUNECONSOLE_TEST_LIBRARY', ''))
    if (os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1' or database != Path('/library/state.db')
            or output.parent.resolve() != Path('/reports')):
        raise ValueError('Use test-library.py with a private writable snapshot and /reports output')
    original_qualification, started = preparation.QUALIFICATIONS, time.monotonic()
    report = {'candidate_selected': False, 'operational_acceptance': False,
        'limits': ['Controlled warm-corpus Relax fit, bypassing ordinary pending-work scheduling.',
            'Actual target metadata and originals; no human quality evaluation.',
            'One bounded batch is not full-corpus refresh, physical I/O or retention acceptance.',
            'Restore covers model and provenance domains only; SQL backup is explicit recovery evidence.']}
    sources = ('experiments/audio_lab_tools/vibe_pooling_artifacts.py',
        'experiments/audio_lab_tools/vibe_pooling_operations.py', 'tests/file_custody_support.py',
        'src/yt_playlist/core/vibe_model_files.py', 'src/yt_playlist/core/vibe_provenance.py',
        'src/yt_playlist/repos/vibe_models.py', 'src/yt_playlist/repos/vibe_predictions.py',
        'src/yt_playlist/rec/vibe_joint_model.py', 'src/yt_playlist/rec/vibe_joint_preparation.py')
    repository = Path(__file__).resolve().parents[2]
    report['code'] = {}
    with zipfile.ZipFile(output.parent / 'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in sources:
            raw = (repository / name).read_bytes()
            report['code'][name] = hashlib.sha256(raw).hexdigest()
            archive.writestr(name, raw)
    store = Store(database)
    restored = None
    try:
        store.init_schema()
        report['before'] = storage(store)
        preparation.QUALIFICATIONS = {preparation.config_identity(): 'isolated-artifact-rehearsal'}
        at = time.monotonic()
        assert preparation.step(store)
        report['fit_and_publication_seconds'] = time.monotonic() - at
        state = preparation.state(store, 'relax')
        report['state'] = state
        if state.get('outcome') != 'ready':
            report['outcome'] = 'fit-deferred'
            return
        generation = store.settings.get_setting(vibe_models.HEAD_PREFIX + 'relax')
        value = store.vibe_predictions.joint_model(generation)
        manifest = {row['recording'] for row in value['inputs']}
        candidates = list(store.conn.execute('SELECT r.identity_key,v.recording_id,v.artist_key,v.subgenre_key '
            'FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id '
            "WHERE v.vibe='relax' AND v.measurement_id IS NULL AND v.artist_key IS NOT NULL "
            'ORDER BY r.id LIMIT 128'))
        posterior = model.Posterior(value)
        tracks, resolved = {}, {}
        albums = store.vibe_predictions.joint_albums([row[0] for row in candidates])
        for row in candidates:
            key, recording, artist, genre = row
            assert recording not in manifest
            estimate = posterior.predict(json.loads(artist), genre, album=albums.get(key), recording=recording)
            if estimate is None:
                continue
            tracks[key] = {'evidence': None, 'artist': artist, 'genre': genre,
                           'version': vibe_preparation.version()}
            resolved[key] = {vibe: {'retain': True} for vibe, in store.conn.execute(
                'SELECT vibe FROM recording_vibes WHERE recording_id=?', (recording,))}
            resolved[key]['relax'] = {**estimate, 'generation': generation, 'model_version': model.VERSION,
                'conditioning': {'genre': genre, 'album': albums.get(key)}}
        assert len(tracks) >= 20
        def publish():
            with store.read_snapshot() as snapshot:
                prepared = snapshot.store.vibe_predictions.prepare_publication(tracks, resolved, [], time.time())
                try:
                    assert snapshot.publish(lambda: store.vibe_predictions.publish_prepared(prepared))
                finally:
                    prepared.close()
        at = time.monotonic()
        publish()
        report['score_batch_seconds'] = time.monotonic() - at
        first_ids = dict(store.conn.execute('SELECT recording_id,prediction_id FROM recording_vibes '
            "WHERE vibe='relax' AND prediction_id IN (SELECT prediction_id FROM prediction_model_generations)"))
        publish()
        assert first_ids == dict(store.conn.execute('SELECT recording_id,prediction_id FROM recording_vibes '
            "WHERE vibe='relax' AND prediction_id IN (SELECT prediction_id FROM prediction_model_generations)"))
        report['unchanged_publication_reuses_ids'] = True
        report['targets'] = len(tracks)
        report['original_recordings'] = len(manifest)
        report['generation'] = generation
        report['after_batch'] = storage(store)
        assert report['after_batch']['rows']['recording_vibes'] == report['before']['rows']['recording_vibes']
        assert report['after_batch']['rows']['prediction_inputs'] == report['before']['rows']['prediction_inputs']
        assert report['after_batch']['rows']['prediction_model_generations'] - report['before']['rows'][
            'prediction_model_generations'] == len(tracks)
        model_root = vibe_models.directory(store.conn)
        path = model_root / vibe_models.relative(generation)
        raw = fao.read(path).bytes
        report['model_bytes'] = len(raw)
        fao.write(path, b'controlled corrupt model')
        try:
            store.vibe_predictions.joint_model(generation)
        except fao.CorruptFile:
            report['corrupt_required_model_rejected'] = True
        else:
            raise AssertionError('Corrupt model accepted')
        repaired, staged = store.vibe_predictions.stage_joint_model(value)
        assert repaired == generation
        try:
            staged.publish()
        finally:
            staged.close()
        assert fao.read(path).bytes == raw
        pid = next(iter(first_ids.values()))
        expected = vibe_provenance.read(store.conn, pid)
        recording = next(recording for recording, prediction in first_ids.items() if prediction == pid)
        key = store.conn.execute('SELECT identity_key FROM recordings WHERE id=?', (recording,)).fetchone()[0]
        assert expected['conditioning'] == resolved[key]['relax']['conditioning']
        report['target_conditioning_preserved'] = True
        # An explicit recovery experiment may copy SQL. Ordinary implementation
        # checks still obtain their CoW snapshot exclusively from the manager.
        # The bounded library image need not have space for a second full SQL
        # database. Recovery's disposable copy belongs on the report filesystem.
        with tempfile.TemporaryDirectory(prefix='819-shared-restore-', dir=output.parent) as temporary:
            root = Path(temporary)
            # This is an explicit recovery export, outside the request path.
            # Bound its full SQL backup separately from foreground read budgets.
            with store.read_view(seconds=120) as reader:
                models = export_owner(reader.conn, vibe_models, root / 'saved-models')
                receipts = export_owner(reader.conn, vibe_provenance, root / 'saved-receipts')
                restored = Store(root / 'restored.db')
                reader.conn.backup(restored.conn)
            store.conn.close()
            report['producer_closed_before_restore'] = True
            shutil.rmtree(model_root)
            restore_owner(restored.conn, 'vibe-provenance', vibe_provenance, receipts)
            try:
                vibe_provenance.read(restored.conn, pid)
            except fao.CorruptFile:
                report['restore_requires_saved_model_bytes'] = True
            else:
                raise AssertionError('Restore succeeded without required model')
            restore_owner(restored.conn, 'vibe-models', vibe_models, models)
            assert vibe_provenance.read(restored.conn, pid) == expected
            assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
            report['independent_domain_restore'] = True
            report['restored_input_count'] = len(expected['inputs'])
            restored.conn.close()
            restored = None
        report['temporary_recovery_workspace_removed'] = True
        report['source_pins_unchanged'] = all(hashlib.sha256((repository / name).read_bytes()).hexdigest() == digest
                                             for name, digest in report['code'].items())
        report['model_sha256'] = hashlib.sha256(raw).hexdigest()
        report['peak_process_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        report['outcome'] = 'controlled-rehearsal-complete'
    finally:
        preparation.QUALIFICATIONS = original_qualification
        if restored is not None:
            restored.conn.close()
        store.conn.close()
        report['seconds'] = time.monotonic() - started
        output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(json.dumps({key: report[key] for key in ('outcome', 'targets', 'model_bytes', 'seconds')},
                     sort_keys=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    run(args.output)
