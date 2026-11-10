"""Read-only replay receipts and bounded fresh-library consumer observations."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import random
import resource
import socket
import sqlite3
import sys
import tempfile
import time

import numpy as np
from pytest import MonkeyPatch

from . import consumer_replay, library_replay, paired_campaign, replay_runtime, replay_storage
from ..support.external_experiment import ROOT, digest, save, stamp


def verify_paired(target):
    """Verify archived provenance without retrying a failed or interrupted slice."""
    target = Path(target).resolve()
    if not target.is_relative_to(library_replay.WORK.resolve()) or not target.name.startswith('paired-slices-'):
        raise ValueError('Expected an experiments-only paired-slices campaign')
    plan = json.loads((target / 'plan.json').read_text())
    state = json.loads((target / 'progress.json').read_text())
    report = json.loads((target / 'report.json').read_text())
    if state['plan_sha256'] != digest(target / 'plan.json') or report['plan_sha256'] != state['plan_sha256']:
        raise ValueError('Consumer campaign plan changed')
    paired_campaign.verify_code_archive(target, plan['code_sha256'])
    if replay_runtime.runtime_hashes() != plan['runtime_code_sha256']:
        raise ValueError('Frozen production runtime changed')
    if replay_runtime.runtime_identity() != {key: plan[key] for key in replay_runtime.runtime_identity()}:
        raise ValueError('Consumer audit requires the original runtime snapshot')
    if digest(target / 'source.db') != plan['frozen_database_sha256']:
        raise ValueError('Frozen consumer database changed')
    replay_storage.verify(target, plan['payload_manifest_sha256'])
    if digest(library_replay.RUN / 'inputs/manifest.json') != plan['input_manifest_sha256']:
        raise ValueError('Frozen consumer input manifest changed')
    candidates = library_replay.frozen_models()
    for name, model in plan['models'].items():
        if digest(candidates[name]) != model['sha256']:
            raise ValueError('Frozen consumer candidate changed: ' + name)
    if state['fixed_radio_seed_sha256'] != paired_campaign.seed_digest(state['fixed_radio_seed']):
        raise ValueError('Common Radio seed changed')
    if state['methods'] != report['methods'] or state['attempts'] != report['attempts']:
        raise ValueError('Consumer campaign state and report disagree')
    if not report['runtime_code_unchanged_during_run']:
        raise ValueError('Historical source guard did not pass')
    paired_campaign.verify_receipts(target, state['methods'])
    expected = [name for name, _method in paired_campaign.CHOICES]
    if list(state['methods']) != expected[:len(state['methods'])]:
        raise ValueError('Completed candidates violate the fixed order')
    latest = {row['candidate']: row for row in state['attempts']}
    paths = {name: target / row['output'] for name, row in latest.items()
             if (target / row['output'] / 'candidate-mixes.json').is_file()
             and (target / row['output'] / 'candidate-radio.json').is_file()}
    partial = library_replay.compare_outputs(target, list(paths), paths=paths) if paths else {}
    result = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'plan_sha256': state['plan_sha256'], 'report_sha256': digest(target / 'report.json'),
        'archived_research_code_verified': True, 'runtime_sql_models_payloads_receipts_verified': True,
        'accepted_candidates': list(state['methods']),
        'latest_attempts': {name: {'status': row['status'], 'error': row.get('error'), 'output': row['output']}
                            for name, row in latest.items()},
        'all_fixed_candidates_attempted': all(latest.get(name, {}).get('status') in ('complete', 'failed')
                                             for name in expected),
        'all_fixed_candidates_passed': report['complete'],
        'current_research_code_differs_from_archive': [name for name, value in plan['code_sha256'].items()
                                                      if digest(ROOT / name) != value],
        'partial_output_comparisons': partial,
        'partial_output_files_sha256': {str(path.relative_to(target)): digest(path)
            for name, output in paths.items() if name not in state['methods']
            for path in sorted(output.iterdir()) if path.is_file()},
        'policy': 'Read-only verification of retained inputs and accepted receipts. Failed attempts are never promoted; partial playlist comparisons are descriptive only. No consumer retry, inference fit or label scoring.'}
    save(target / 'verification.json', result)
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ('partial_output_comparisons', 'partial_output_files_sha256')}), flush=True)
    return result


def playlist_sequences(output):
    mixes = json.loads((output / 'candidate-mixes.json').read_text())
    radios = json.loads((output / 'candidate-radio.json').read_text())
    return {**{'mix:' + vibe + ':' + card['lane']: [row['key'] for row in card['tracks']]
               for vibe, cards in mixes.items() for card in cards},
            **{'radio:' + dial: [row['key'] for row in tracks] for dial, tracks in radios.items()}}


def finish_constraints(target, *, independent_execution):
    """Complete missing restrictions without retrying the failed warm operation."""
    from yt_playlist.rec import audio_vibe_model, musical_character
    from yt_playlist.rec.radio_candidates import RadioCandidates
    target = Path(target).resolve()
    verified = verify_paired(target)
    failed = [(name, row) for name, row in verified['latest_attempts'].items() if row['status'] == 'failed']
    if len(failed) != 1 or verified['all_fixed_candidates_passed']:
        raise ValueError('Expected exactly one retained failed candidate')
    name, attempt = failed[0]
    failed_output = target / attempt['output']
    previous = json.loads((failed_output / 'report.json').read_text())
    stopped = previous['measurements'].get('radio_retune_candidates_and_four_directions:12', {})
    if (not previous.get('radio_track_counts') or not previous.get('mix_track_counts')
            or 'WorkClosing' not in attempt['error'] or stopped.get('wall_s', 0) < stopped.get('limit_s', float('inf'))):
        raise ValueError('Continuation requires the retained retune-13 deadline failure with complete playlists')
    plan = json.loads((target / 'plan.json').read_text())
    state = json.loads((target / 'progress.json').read_text())
    database = target / 'source.db'
    library, samples = library_replay.frozen_inputs()
    candidates = library_replay.frozen_models()
    checkpoint = json.loads(candidates['checkpoint'].read_text())
    acoustic, _excluded = library_replay.acoustic_inputs(library, samples, checkpoint['embedding_signature'])
    metadata, observations, audit = library_replay.candidate_observations(library, acoustic,
        json.loads(candidates[name].read_text()), plan['models'][name]['method'])
    adapter, calls = library_replay.consumer_adapter(musical_character.load, library, metadata, observations)
    output = target / ('constraint-followup-' + stamp().replace(':', '').replace('+', '_'))
    code = {**paired_campaign.code_hashes(), str(Path(__file__).relative_to(ROOT)): digest(__file__)}
    report = {'created_at': stamp(), 'candidate': name, 'original_attempt': attempt,
        'original_report_sha256': digest(failed_output / 'report.json'),
        'plan_sha256': digest(target / 'plan.json'), 'research_code_sha256': code,
        'execution_mode': 'independent_experiment',
        'warm_retunes_retried': False, 'original_failure_retained': True, 'complete': False,
        'policy': 'Republish the same frozen candidate to complete missing restrictions. Require exactly matching ordered playlists. The warm deadline failure and incomplete original campaign remain unchanged.'}
    seed_offer = RadioCandidates.seed_offer
    def common_seed(candidate, count=16):
        if count != 1:
            return seed_offer(candidate, count)
        tracks = deepcopy(state['fixed_radio_seed'])
        profiles = musical_character.load(candidate.store, [row['key'] for row in tracks])
        for row in tracks:
            row['musical'] = profiles.get(row['key'])
        candidate.rng.seed(136)
        return tracks
    before = replay_runtime.runtime_hashes()
    try:
        with tempfile.TemporaryDirectory(prefix='constraint-private-', dir=target) as temporary, MonkeyPatch.context() as patch:
            payload_calls = replay_storage.guard_reads(patch, target)
            random.seed(136)
            np.random.seed(136)
            patch.setenv('TUNECONSOLE_BENCH_DB', str(database))
            patch.setenv('TUNECONSOLE_MUSICAL_REPORT_DIR', str(output))
            patch.setenv('TUNECONSOLE_BENCH_NOW', '1793260800')
            patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(candidates['checkpoint']))
            patch.setattr(musical_character, 'load', adapter)
            patch.setattr(RadioCandidates, 'seed_offer', common_seed)
            consumer_replay.evaluate(Path(temporary), patch, publication_seconds=plan['publication_limit_s'],
                diagnostics={'profile_adapter': calls, 'payload_reads': payload_calls}, warm_samples=0)
            report['ordered_playlists_match_failed_attempt'] = playlist_sequences(output) == playlist_sequences(failed_output)
            if not report['ordered_playlists_match_failed_attempt']:
                raise ValueError('Constraint continuation changed the original ordered playlists')
            report['constraints'] = library_replay.consumer_constraints(Path(temporary) / 'musical-library.db', output,
                plan['explicit_genre_families'], common_seed_track=state['fixed_radio_seed'][0])
        report.update(complete=True, observation_audit=audit, consumer_calls=calls, payload_reads=payload_calls)
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        output.mkdir(exist_ok=True)
        report['runtime_and_research_code_unchanged'] = before == replay_runtime.runtime_hashes() and all(
            digest(ROOT / relative) == value for relative, value in code.items())
        report['sql_unchanged'] = digest(database) == plan['frozen_database_sha256']
        replay_storage.verify(target, plan['payload_manifest_sha256'])
        report['output_sha256'] = {path.name: digest(path) for path in output.iterdir() if path.is_file()}
        report['complete'] &= report['runtime_and_research_code_unchanged'] and report['sql_unchanged']
        save(output / 'followup.json', report)
    print(json.dumps({'constraint_followup': str(output), **report}), flush=True)


def snapshot_metadata(database, keys):
    """Read selected genre/year values from a pinned private SQL snapshot only."""
    database = Path(database).resolve()
    if not database.is_relative_to(library_replay.WORK.resolve()):
        raise ValueError('Fresh metadata must come from an experiments-only SQL snapshot')
    plan = json.loads((database.parent / 'plan.json').read_text())
    expected = plan['frozen_database_sha256']
    if digest(database) != expected:
        raise ValueError('Frozen metadata database changed')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.row_factory = sqlite3.Row
        rows = {}
        for row in conn.execute('SELECT identity_key,genre,mb_year FROM catalog_tracks ORDER BY id'):
            if row['identity_key'] in keys:
                rows.setdefault(row['identity_key'], {'genre': row['genre'], 'year': row['mb_year']})
    return rows, expected


def fresh_consumers(database, *, seconds, independent_execution):
    """Observe actual consumers after real membership, metadata and captures arrive."""
    from yt_playlist.core.activity import cancellable_work, WorkClosing, WorkBudgetExceeded
    from yt_playlist.core.store import Store
    from yt_playlist.rec import (audio_vibe_model, home_vibe, mode_surfaces, musical_character,
                                 musical_preparation, progressive, radio_tuning, vibe_preparation, vibe_pools)
    from yt_playlist.rec.radio_candidates import RadioCandidates

    if not 0 < seconds <= 120:
        raise ValueError('Fresh consumer phases require a bound above zero and at most 120 seconds')
    if not replay_runtime.runtime_identity()['runtime_snapshot']:
        raise ValueError('Fresh consumers require an explicit frozen runtime')
    library, samples = library_replay.frozen_inputs()
    checkpoint_path = library_replay.frozen_models()['checkpoint']
    checkpoint = json.loads(checkpoint_path.read_text())
    acoustic, excluded = library_replay.acoustic_inputs(library, samples, checkpoint['embedding_signature'])
    records = {row['id']: row for row in library['recordings']}
    selected = {row['recording_id']: set(row['sample_ids']) for row in acoustic}
    metadata, database_sha = snapshot_metadata(database, {records[rid]['identity_key'] for rid in selected})
    inputs = [(row, sample) for rid, values in samples.items() if rid in selected
              for row, sample in values if row['id'] in selected[rid]]
    before = replay_runtime.runtime_hashes()
    target = library_replay.WORK / ('fresh-consumers-' + stamp().replace(':', '').replace('+', '_'))
    target.mkdir(mode=0o700, parents=True)
    started, admission, phases = time.monotonic(), 0, {}
    report = {'created_at': stamp(), 'script_sha256': digest(__file__), **replay_runtime.runtime_identity(),
        'runtime_code_sha256': before, 'checkpoint_sha256': digest(checkpoint_path),
        'input_manifest_sha256': digest(library_replay.RUN / 'inputs/manifest.json'),
        'metadata_database': str(Path(database).resolve()), 'metadata_database_sha256': database_sha,
        'copied_selected_metadata': metadata, 'recordings': len(selected), 'excluded_captures': excluded,
        'execution_mode': 'independent_experiment',
        'phases': phases, 'first_observed_nonempty_s': {}, 'complete': False,
        'limitations': ['77-recording captured cohort, not full account import or signed-in acceptance.',
            'No playback history, collaborative vectors, development annotations or network enrichment.',
            'Copied selected genre/year values are controlled replay inputs; original lookup validity is not re-established.',
            'First output means nonempty consumer output, not human-confirmed musical suitability.',
            'Observations occur at worker boundaries, so first output times are upper bounds at those boundaries.',
            'Worker admission clock advances five seconds per slice; wall measurements exclude scheduling idle time.',
            'Consumer probes themselves consume wall time and may overlap other agents on this host.']}
    save(target / 'report.json', report)
    keys, entries = {}, {}
    with MonkeyPatch.context() as patch:
        def no_network(*args, **kwargs):
            raise AssertionError('Fresh consumer replay attempted a network connection')
        patch.setattr(socket.socket, 'connect', no_network)
        patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(checkpoint_path))
        random.seed(136)
        np.random.seed(136)
        store = Store(target / 'library.db')

        def observe(stage, name):
            wall, cpu = time.monotonic(), time.process_time()
            observation = {'boundary': name, 'elapsed_s': wall - started}
            try:
                with cancellable_work(lambda: time.monotonic() - wall > 30):
                    now = 1793260800. + admission * 5
                    bundles = mode_surfaces.prepare_bundles(store, now)
                    mixes = {vibe: vibe_pools.assemble(store, now, vibe, bundles=bundles)
                             for vibe in home_vibe.PRESETS}
                    radios = {}
                    for intensity in (0, .5, 1):
                        with closing(RadioCandidates(store, now,
                                {**radio_tuning.DEFAULTS, 'energy': intensity}, comparison=None)) as candidate:
                            candidate.rng.seed(136)
                            tracks = list(candidate.seed_offer(1))
                            while tracks and len(tracks) < 24:
                                following = candidate.next_track(tracks)
                                if following is None:
                                    break
                                tracks.append(dict(following))
                            radios[str(intensity)] = tracks
                    observation.update(mix_track_counts={vibe: {card['lane']: len(card['tracks']) for card in cards}
                                                         for vibe, cards in mixes.items()},
                                       radio_track_counts={dial: len(tracks) for dial, tracks in radios.items()})
                    save(target / (stage['name'] + '-' + name + '-outputs.json'), {'mixes': mixes, 'radio': radios})
                    for surface, count in [('mix:' + vibe, sum(lanes.values()))
                                           for vibe, lanes in observation['mix_track_counts'].items()] + [
                                           ('radio:' + dial, count) for dial, count in observation['radio_track_counts'].items()]:
                        if count:
                            report['first_observed_nonempty_s'].setdefault(surface, time.monotonic() - started)
            except (WorkClosing, WorkBudgetExceeded) as error:
                observation['error'] = repr(error)
            observation.update(wall_s=time.monotonic() - wall, cpu_s=time.process_time() - cpu)
            stage['observations'].append(observation)
            save(target / 'report.json', report)

        def drain(name):
            nonlocal admission
            wall, cpu = time.monotonic(), time.process_time()
            stage = phases[name] = {'name': name, 'observations': [], 'drained': False}
            previous = None
            for step in range(2048):
                if time.monotonic() - wall > seconds:
                    break
                with cancellable_work(lambda: time.monotonic() - wall > seconds + 5):
                    preparation.capture(1793260800. + admission * 5)
                    preparation.fit(1793260800. + admission * 5)
                admission += 1
                status = preparation.status()
                counts = (store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0],
                          status['library_vectors'])
                pending = (store.prepared_content.has_pending() or musical_preparation.due(store)
                           or vibe_preparation.due(store))
                if counts != previous or not pending:
                    observe(stage, 'worker-' + str(step + 1))
                    previous = counts
                if not pending:
                    stage['drained'] = True
                    break
            profiles = musical_character.load(store, list(keys.values()))
            stage.update(wall_s=time.monotonic() - wall, cpu_s=time.process_time() - cpu, steps=step + 1,
                status=preparation.status(), sources=dict(Counter(fit['source'] for profile in profiles.values()
                    for fit in profile['vibes']['fits'].values())),
                library_members=len(store.rec_query.library_keys()),
                samples=store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0],
                measurements=store.conn.execute('SELECT COUNT(*) FROM measurements').fetchone()[0])
            save(target / 'report.json', report)
            print(json.dumps({'fresh_consumer_phase': name, 'drained': stage['drained'],
                              'wall_s': stage['wall_s'], 'sources': stage['sources'],
                              'last_observation': stage['observations'][-1] if stage['observations'] else None}), flush=True)

        try:
            store.init_schema()
            report['initial_counts'] = {table: store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('recordings', 'sample_attributes', 'measurements', 'predictions',
                              'recording_embeddings', 'rec_musical_profiles', 'audio_annotations')}
            if any(report['initial_counts'].values()):
                raise ValueError('Fresh consumers unexpectedly contain existing state')
            preparation = progressive.Preparation(store)
            account = store.identities.upsert_identity('Experiment cohort', 'unused-research-credential', None, True)
            for rid in sorted(selected):
                sample = next(sample for row, sample in inputs if row['recording_id'] == rid)
                row = records[rid]
                entries[rid] = store.tracks.upsert_track(sample['video_id'], row['title'], row['artist'],
                                                        None, None, library=False)
                keys[rid] = store.tracks.identity_key_for_video(sample['video_id'])
            playlist = 'experiment-captured-cohort'
            result = store.playlists.publish_playlist_snapshot(account, playlist, 'Captured research cohort',
                list(entries.values()), 1793260800.,
                expected_revision=store.playlists.playlist_revision(account, playlist))
            if result is None or len(store.rec_query.library_keys()) != len(selected):
                raise ValueError('Fresh account membership did not import the selected cohort')
            drain('title_artist_only')
            for rid, entry in entries.items():
                fields = metadata.get(records[rid]['identity_key'], {})
                store.enrichment.apply_provider_fields(entry, 'frozen_catalog_replay', fields)
            drain('copied_genre_year')
            for row, sample in inputs:
                if row['point'] == 'thirty':
                    store.audio_vibes.save_sample(sample, row['captured_at'])
            drain('one_excerpt')
            for row, sample in inputs:
                if row['point'] != 'thirty':
                    store.audio_vibes.save_sample(sample, row['captured_at'])
            drain('three_excerpts')
            report['foreign_key_errors'] = [tuple(row) for row in store.conn.execute('PRAGMA foreign_key_check')]
            report['final_development_annotations'] = store.conn.execute('SELECT COUNT(*) FROM audio_annotations').fetchone()[0]
            report['final_cf_vectors'] = len(store.rec_model.get_rec_vectors())
            report['complete'] = all(stage['drained'] for stage in phases.values()) and not (
                report['foreign_key_errors'] or report['final_development_annotations'] or report['final_cf_vectors'])
        except BaseException as error:
            report['error'] = repr(error)
            raise
        finally:
            store.conn.close()
            report['wall_s'] = time.monotonic() - started
            report['worker_admission_clock_s'] = admission * 5
            report['process_lifetime_peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
                1 if sys.platform == 'darwin' else 1024)
            report['runtime_code_unchanged_during_run'] = before == replay_runtime.runtime_hashes()
            report['metadata_database_unchanged'] = database_sha == digest(database)
            report['complete'] &= report['runtime_code_unchanged_during_run'] and report['metadata_database_unchanged']
            save(target / 'report.json', report)
    print(json.dumps({'fresh_consumers': str(target), 'complete': report['complete'],
                      'first_observed_nonempty_s': report['first_observed_nonempty_s']}), flush=True)
