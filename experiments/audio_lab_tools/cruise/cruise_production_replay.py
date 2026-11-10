"""Replay production Cruise inference and preparation on frozen real captures.

No model adapters or fabricated extractor identities. All database writes go to
a new, isolated database under experiments; the user database is never opened.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import socket
import time

from pytest import MonkeyPatch
from threadpoolctl import threadpool_limits

from yt_playlist.core import sample_files
from yt_playlist.core.store import Store
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_model as shared
from yt_playlist.rec import audio_vibe_prediction as predictor, home_vibe, musical_character
from yt_playlist.rec import musical_preparation, progressive, vibe_preparation
from .cruise_export import captures
from ..support.external_experiment import digest, save, stamp
from ..studies.vibe_specialist_round import inside, versions


def code_hashes():
    # Whole product source is checked so concurrent edits cannot be concealed by
    # checking only the new scorer. Cached bytecode and unrelated assets excluded.
    root = Path(shared.__file__).resolve().parents[1]
    return {str(path.relative_to(root)): digest(path) for path in sorted(root.rglob('*.py'))}


def replay(directory, seconds):
    if directory.exists():
        raise ValueError('Choose a new replay directory to preserve earlier evidence')
    directory.mkdir(parents=True)
    artifact = cruise.load(cruise.DEFAULT_PATH)
    grouped, inputs, skipped = captures(artifact['header']['embedding_signature'])
    before = code_hashes()
    report = {'created_at': stamp(), 'runtime': versions(), 'code_sha256': before,
              'script_sha256': digest(__file__), 'inputs': inputs,
              'cruise_sha256': digest(cruise.DEFAULT_PATH), 'shared_sha256': digest(shared.DEFAULT_PATH),
              'playback_groups': len(grouped), 'skipped_incompatible': skipped,
              'phases': {}, 'complete': False,
              'limits': ['Real captured audio with original title/artist metadata; no provider or collaborative history.',
                         'This isolated cohort does not establish a full-library migration or complete Radio/Mix outputs.',
                         'Elapsed work time excludes normal scheduling idle time between worker slices.',
                         'No new accuracy evaluation or listening judgments.']}
    save(directory / 'report.json', report)
    started = time.monotonic()
    with MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(directory / 'app-home'))
        patch.setenv(shared.ENVIRONMENT_KEY, str(shared.DEFAULT_PATH))
        patch.setenv(cruise.ENVIRONMENT_KEY, str(cruise.DEFAULT_PATH))
        patch.setenv(cruise.MODE_KEY, 'off')
        def no_network(*args, **kwargs):
            raise AssertionError('Production capture replay attempted network access')
        patch.setattr(socket.socket, 'connect', no_network)
        cases, maximum_error = 0, 0.
        for rows in grouped.values():
            for chosen in (rows[:1], rows):
                original = deepcopy(chosen)
                baseline = predictor.predict(chosen, require_complete=False)
                patch.setenv(cruise.MODE_KEY, 'active')
                active = predictor.predict(chosen, require_complete=False)
                patch.setenv(cruise.MODE_KEY, 'shadow')
                shadow = predictor.predict(chosen, require_complete=False)
                patch.setenv(cruise.MODE_KEY, 'off')
                scores = {row['vibe']: row['score'] for row in baseline['ranking']}
                if shadow['suitability'] != scores or {k: v for k, v in active.items()
                        if k not in ('suitability', 'individual_models')} != baseline:
                    raise ValueError('Production classification or shadow scores changed')
                if any(active['suitability'][v] != scores[v] for v in shared.LABELS if v != 'cruise'):
                    raise ValueError('Production changed another vibe')
                candidate = cruise.score(artifact, [row['embedding']['values'] for row in chosen])
                maximum_error = max(maximum_error, abs(candidate['score'] - active['suitability']['cruise']))
                if chosen != original:
                    raise ValueError('Production inference rewrote captured evidence')
                cases += 1
        report['preview'] = {'cases': cases, 'maximum_cruise_error': maximum_error,
                             'changed_non_cruise_scores': 0, 'changed_classifications': 0}
        store = Store(directory / 'library.db')
        admission = 0
        keys = {}

        def drain(name):
            nonlocal admission
            worker = progressive.Preparation(store)  # Unmodified production budgets.
            start = time.monotonic()
            stage = report['phases'][name] = {'complete': False, 'max_capture_wall_s': 0., 'first_profiles_s': None}
            while time.monotonic() - start < seconds:
                if not (store.prepared_content.has_pending() or musical_preparation.due(store) or vibe_preparation.due(store)):
                    stage['complete'] = True
                    break
                tick = time.monotonic()
                worker.capture(1793260800. + admission * 5)
                admission += 1
                stage['max_capture_wall_s'] = max(stage['max_capture_wall_s'], time.monotonic() - tick)
                count = store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0]
                if count and stage['first_profiles_s'] is None:
                    stage['first_profiles_s'] = time.monotonic() - start
            stage.update(wall_s=time.monotonic() - start, measurements=store.conn.execute(
                'SELECT COUNT(*) FROM measurements').fetchone()[0], sources=dict(store.conn.execute(
                'SELECT source,COUNT(*) FROM recording_vibes GROUP BY source')))
            save(directory / 'report.json', report)
            if not stage['complete']:
                raise ValueError('Production preparation did not converge: ' + name)
            return musical_character.load(store, sorted(set(keys.values())))

        try:
            store.init_schema()
            # Reconstruct every actual captured title/artist, with original IDs
            # and all observations, but without labels or prior prepared state.
            for (video, _run), rows in sorted(grouped.items()):
                row = rows[0]
                store.tracks.upsert_track(video, row['title'], row['artist'], None, None)
                keys[video] = store.tracks.identity_key_for_video(video)
            drain('metadata_only')
            frozen_rows = sorted((row for rows in grouped.values() for row in rows), key=lambda row: row['id'])
            for now, row in enumerate(frozen_rows, 1):
                if row['point'] == 'thirty':
                    store.audio_vibes.save_sample(row, now)
            drain('first_excerpts')
            for now, row in enumerate(frozen_rows, 1):
                if row['point'] != 'thirty':
                    store.audio_vibes.save_sample(row, now)
            baseline = drain('shared_complete_captures')
            patch.setenv(cruise.MODE_KEY, 'active')
            active = drain('cruise_rollout')
            changed, measured = 0, 0
            for key in baseline:
                old, new = baseline[key]['vibes']['fits'], active[key]['vibes']['fits']
                changed += old['cruise']['score'] != new['cruise']['score']
                if any(old[v] != new[v] for v in shared.LABELS if v != 'cruise'):
                    raise ValueError('Preparation changed a non-Cruise fit')
                if old['cruise']['source'] == 'audio_measurement':
                    captured = store.vibe_predictions.inputs([key])[key]
                    expected = predictor.predict(captured['samples'])['suitability']['cruise']
                    if abs(new['cruise']['score'] - expected) > 1e-12:
                        raise ValueError('Published recording fit differs from production inference')
                    measured += 1
                if home_vibe.affinity({'key': key}, 'cruise', active) != new['cruise']['score']:
                    raise ValueError('Home did not consume the prepared Cruise score')
                if musical_character.intensity_evidence({'musical': baseline[key]}) != musical_character.intensity_evidence({'musical': active[key]}):
                    raise ValueError('Cruise altered shared radio intensity')
            active_ids = {row[0] for row in store.conn.execute('SELECT id FROM measurements')}
            patch.setenv(cruise.MODE_KEY, 'off')
            rolled_back = drain('rollback')
            if any(rolled_back[key]['vibes']['fits'] != baseline[key]['vibes']['fits'] for key in baseline):
                raise ValueError('Rollback did not restore shared scores')
            if not active_ids <= {row[0] for row in store.conn.execute('SELECT id FROM measurements')}:
                raise ValueError('Rollback discarded immutable history')
            if list(store.conn.execute('PRAGMA foreign_key_check')):
                raise ValueError('Production replay has invalid lineage')
            for row in store.conn.execute('SELECT id,run_id,point,video_id,filename FROM sample_attributes'):
                sample_files.read(row['filename'], expected={k: row[k] for k in ('id', 'run_id', 'point', 'video_id')})
            report.update(recordings=len(baseline), measured_recordings=measured,
                          changed_cruise_recordings=changed, rollback_exact=True, complete=True)
        finally:
            store.conn.close()
            report['runtime_code_unchanged'] = before == code_hashes()
            report['complete'] &= report['runtime_code_unchanged']
            report['wall_s'] = time.monotonic() - started
            save(directory / 'report.json', report)
    if not report['complete']:
        raise ValueError('Runtime changed during replay; preserve report and repeat with stable source')
    print(json.dumps({key: report[key] for key in ('complete', 'recordings', 'measured_recordings',
        'changed_cruise_recordings', 'preview', 'rollback_exact', 'wall_s')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=60, help='Maximum elapsed work per phase')
    parser.add_argument('--independent-execution', action='store_true',
                        help='Compatibility option; research always runs independently of pytest')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        replay(inside(args.directory), args.seconds)


if __name__ == '__main__':
    main()
