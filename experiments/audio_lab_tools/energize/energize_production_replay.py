"""Exercise Energize adoption on real captures in an isolated fresh database."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import socket
import time

from pytest import MonkeyPatch
from threadpoolctl import threadpool_limits

from yt_playlist.core import features
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS, INTENSITY_RELEASE
from yt_playlist.core.store import Store
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release, audio_vibe_relax as relax
from yt_playlist.rec import audio_vibe_uplift as uplift, audio_vibe_energize as energize
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor
from yt_playlist.rec import home_vibe, musical_character, musical_preparation, progressive, vibe_preparation
from ..support.external_experiment import digest, save, stamp
from ..focus.focus_experiment import inside, runtime
from .energize_export import captures, LOCAL


def code_hashes():
    root = Path(shared.__file__).resolve().parents[1]
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*.py'))}


def replay(directory, seconds):
    if directory.exists():
        raise ValueError('Choose a new replay directory')
    directory.mkdir(parents=True)
    captured = captures()
    before = code_hashes()
    modules = (shared, cruise, release, relax, uplift, energize)
    models = {m.__name__: digest(m.DEFAULT_PATH) for m in modules}
    report = {'created_at': stamp(), 'runtime': runtime(), 'code_sha256': before,
              'script_sha256': digest(__file__), 'capture_receipt_sha256': digest(LOCAL / 'receipt.json'),
              'models': models, 'phases': {}, 'complete': False,
              'limits': ['Frozen captured cohort, not a full-account or rendered-output review.',
                         'No annotations used for inference or accuracy scoring.',
                         'Work time excludes idle time between normal preparation slices.']}
    save(directory / 'report.json', report)
    with MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(directory / 'app-home'))
        patch.setattr(features, 'LOCAL_ENV', directory / '.env.local')
        for module in modules:
            patch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
        for module in (cruise, release, relax, uplift):
            patch.setenv(module.MODE_KEY, 'active')
        patch.setenv(energize.MODE_KEY, 'off')

        def no_network(*args, **kwargs):
            raise AssertionError('Replay attempted network access')

        patch.setattr(socket.socket, 'connect', no_network)
        cases = 0
        for _metadata, rows in captured:
            for count in sorted({1, min(3, len(rows)), len(rows)}):
                selected = rows[:count]
                untouched = deepcopy(selected)
                baseline = predictor.predict(selected, require_complete=False)
                patch.setenv(energize.MODE_KEY, 'active')
                active = predictor.predict(selected, require_complete=False)
                patch.setenv(energize.MODE_KEY, 'off')
                if {k: v for k, v in baseline.items() if k not in ('suitability', 'individual_models')} != {
                        k: v for k, v in active.items() if k not in ('suitability', 'individual_models')}:
                    raise ValueError('Shared classification changed')
                if any(active['individual_models'][v] != baseline['individual_models'][v] for v in ('cruise', 'release', 'relax', 'uplift')) or any(
                        baseline['suitability'][v] != active['suitability'][v] for v in shared.LABELS if v != 'energize'):
                    raise ValueError('Another vibe changed')
                if 'energize' not in active['individual_models'] or selected != untouched:
                    raise ValueError('Capture was mutated or Energize did not apply')
                cases += 1
        report['preview_cases'] = cases
        store = Store(directory / 'library.db')
        keys, admission = {}, 0

        def drain(name):
            nonlocal admission
            start = time.monotonic()
            worker = progressive.Preparation(store)
            phase = report['phases'][name] = {'complete': False, 'max_slice_seconds': 0., 'first_profiles_seconds': None}
            while time.monotonic() - start < seconds:
                if not (store.prepared_content.has_pending() or musical_preparation.due(store) or vibe_preparation.due(store)):
                    phase['complete'] = True
                    break
                tick = time.monotonic()
                worker.capture(1793376000. + admission * 5)
                admission += 1
                phase['max_slice_seconds'] = max(phase['max_slice_seconds'], time.monotonic() - tick)
                count = store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0]
                if count and phase['first_profiles_seconds'] is None:
                    phase['first_profiles_seconds'] = time.monotonic() - start
            phase['wall_seconds'] = time.monotonic() - start
            save(directory / 'report.json', report)
            if not phase['complete']:
                raise ValueError('Preparation did not converge: ' + name)
            return musical_character.load(store, list(keys.values()))

        try:
            store.init_schema()
            # Reproduce the pre-adoption serving layout: only Release has an
            # internal shared-intensity channel. Energize comes from normal fits.
            patch.setattr(vibe_preparation, 'INTENSITY_CHANNELS', {INTENSITY_RELEASE: 'release'})
            for metadata, _rows in captured:
                video = metadata['video_id']
                store.tracks.upsert_track(video, metadata['title'], metadata['artist'], None, None)
                keys[video] = store.tracks.identity_key_for_video(video)
            drain('metadata_only')
            for _metadata, rows in captured:
                store.audio_vibes.save_sample(rows[0], 1)
            drain('first_excerpts')
            for _metadata, rows in captured:
                for index, row in enumerate(rows[1:], 2):
                    store.audio_vibes.save_sample(row, index)
            baseline = drain('shared_complete_captures')
            patch.setattr(vibe_preparation, 'INTENSITY_CHANNELS', INTENSITY_CHANNELS)
            patch.setenv(energize.MODE_KEY, 'active')
            active = drain('energize_rollout')
            changed, measured = 0, 0
            for key, profile in baseline.items():
                old, new = profile['vibes']['fits'], active[key]['vibes']['fits']
                changed += old['energize']['score'] != new['energize']['score']
                if set(new) != set(shared.LABELS) or any(old[v] != new[v] for v in shared.LABELS if v != 'energize'):
                    raise ValueError('Prepared non-Energize score changed or internal channel leaked')
                if active[key]['vibes']['intensity_fits']['energize'] != old['energize']:
                    raise ValueError('Shared Energize intensity evidence was not preserved')
                if active[key]['vibes']['intensity_fits']['release'] != profile['vibes']['intensity_fits']['release']:
                    raise ValueError('Shared Release intensity evidence changed')
                if musical_character.intensity_evidence({'musical': profile}) != musical_character.intensity_evidence({'musical': active[key]}):
                    raise ValueError('Radio intensity changed')
                if old['energize']['source'] == 'audio_measurement':
                    inputs = store.vibe_predictions.inputs([key])[key]
                    expected = predictor.predict(inputs['samples'])['suitability']['energize']
                    if abs(new['energize']['score'] - expected) > 1e-12:
                        raise ValueError('Published score differs from inference')
                    measured += 1
                if home_vibe.affinity({'key': key}, 'energize', active) != new['energize']['score']:
                    raise ValueError('Home did not use the prepared score')
            history = {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
            patch.setenv(energize.MODE_KEY, 'off')
            rollback = drain('rollback')
            if any(rollback[k]['vibes']['fits'] != baseline[k]['vibes']['fits'] for k in baseline):
                raise ValueError('Rollback differs from the baseline')
            if not history <= {r[0] for r in store.conn.execute('SELECT id FROM measurements')}:
                raise ValueError('Immutable history was discarded')
            if list(store.conn.execute('PRAGMA foreign_key_check')):
                raise ValueError('Invalid measurement lineage')
            report.update(recordings=len(baseline), measured_recordings=measured,
                          changed_energize_recordings=changed, rollback_exact=True,
                          shared_intensity_preserved=True, complete=True)
        finally:
            store.conn.close()
            report['code_unchanged'] = before == code_hashes()
            report['models_unchanged'] = models == {m.__name__: digest(m.DEFAULT_PATH) for m in modules}
            report['complete'] &= report['code_unchanged'] and report['models_unchanged']
            save(directory / 'report.json', report)
    if not report['complete']:
        raise ValueError('Runtime changed during replay')
    print(json.dumps({k: v for k, v in report.items() if k != 'code_sha256'}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        replay(inside(args.directory), args.seconds)


if __name__ == '__main__':
    main()
