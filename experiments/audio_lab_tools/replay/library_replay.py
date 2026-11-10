"""Bounded cold preparation and paired consumer replay on private library copies.

The paired replay supplies frozen candidate observations through the production
propagation policy and musical-profile consumer boundary. It is an experiment
adapter, not a deployable model loader or a test of live invalidation.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import random
import socket
import tempfile
import time

import numpy as np
from pytest import MonkeyPatch
from threadpoolctl import threadpool_limits

from ..studies.external_checkpoint import RUN
from ..support.external_experiment import ROOT, digest, save, stamp
from .external_propagation import cohorts, current_inputs
from ..evaluation.external_transfer import VIBES
from ..studies.heldout_cohort import candidate_predictions, inference_hashes
from .replay_runtime import freeze as freeze_runtime, runtime_hashes, runtime_identity

WORK = RUN / 'library-replay'


def frozen_models():
    """Replay the exact content-addressed artifacts frozen before label scoring."""
    plan = json.loads((RUN / 'reserved-cohort/plan.json').read_text())
    if inference_hashes() != plan['inference_code_sha256']:
        raise ValueError('Frozen candidate inference code changed')
    paths = {}
    for name, candidate in plan['candidates'].items():
        path = Path(candidate['path'])
        if digest(path) != candidate['sha256']:
            raise ValueError('Frozen replay candidate changed: ' + name)
        if parent := candidate.get('shared_model'):
            if digest(parent['path']) != parent['sha256']:
                raise ValueError('Frozen replay shared representation changed: ' + name)
        paths[name] = path
    return paths


def inspect():
    library, samples = frozen_inputs()
    paths = frozen_models()
    checkpoint = json.loads(paths['checkpoint'].read_text())
    acoustic, excluded = acoustic_inputs(library, samples, checkpoint['embedding_signature'])
    result = {'created_at': stamp(), 'captured_recordings': len(samples),
        **runtime_identity(), 'verified_runtime_files': len(runtime_hashes()),
        'complete_compatible_recordings': len(acoustic), 'excluded_incomplete_or_incompatible': excluded,
        'candidate_files_missing': [name for name, path in paths.items() if not path.exists()],
        'library_manifest_sha256': digest(RUN / 'inputs/manifest.json')}
    save(WORK / 'inventory.json', result)
    print(json.dumps(result), flush=True)


def runtime_audit(report_path):
    """Explain source drift without rewriting the original measurement report."""
    report_path = report_path.resolve()
    if not report_path.is_relative_to(WORK.resolve()):
        raise ValueError('Runtime audits require a report inside the replay workspace')
    report = json.loads(report_path.read_text())
    previous, current = report['runtime_code_sha256'], runtime_hashes()
    changed = [{'path': name, 'before_sha256': previous.get(name),
                'current_sha256': current.get(name)}
               for name in sorted(previous.keys() | current.keys())
               if previous.get(name) != current.get(name)]
    result = {'created_at': stamp(), 'report_sha256': digest(report_path),
              'changed_files': changed,
              'policy': 'Compare the recorded pre-run source hashes with current source bytes. This does not establish when a change occurred or repair an incomplete run.'}
    save(report_path.parent / 'runtime-audit.json', result)
    print(json.dumps(result), flush=True)


def frozen_inputs():
    base = RUN / 'inputs'
    manifest = json.loads((base / 'manifest.json').read_text())
    path = base / 'library.json'
    if digest(path) != manifest['files']['library.json']:
        raise ValueError('Frozen library changed')
    library = json.loads(path.read_text())
    samples = defaultdict(list)
    for row in library['sample_attributes']:
        path = Path(row['filename'])
        expected = manifest['files'].get(str(path.relative_to(base)))
        if not expected or digest(path) != expected:
            raise ValueError('Frozen capture changed')
        samples[row['recording_id']].append((row, json.loads(path.read_text())))
    return library, samples


def acoustic_inputs(library, samples, signature):
    from yt_playlist.core import audio_embedding
    rows, excluded = [], []
    for rid, values in sorted(samples.items()):
        runs = defaultdict(list)
        for metadata, sample in values:
            runs[(metadata['run_id'], metadata['video_id'])].append((metadata, sample))
        chosen = None
        for _key, run in sorted(runs.items(), key=lambda item: max(row[0]['captured_at'] for row in item[1]), reverse=True):
            try:
                if len(run) == 3 and {row[0]['point'] for row in run} == {'thirty', 'middle', 'two_thirds'} and all(
                        audio_embedding.signature(sample) == signature for _, sample in run):
                    audio_embedding.vector([sample for _, sample in run])
                    chosen = run
                    break
            except (KeyError, TypeError, ValueError):
                continue
        if chosen is None:
            excluded.append(rid)
            continue
        rows.append({'recording_id': rid, 'sample_ids': [row['id'] for row, _ in chosen],
                     'embedding': audio_embedding.vector([sample for _, sample in chosen])})
    return rows, excluded


def candidate_observations(library, acoustic, artifact, method):
    metadata, observations = current_inputs(library, {'provider_measurement', 'audio_measurement'})
    x = np.asarray([row['embedding'] for row in acoustic])
    values = candidate_predictions(artifact, x)[method]
    replaced = []
    for row, scores in zip(acoustic, values, strict=True):
        rid = row['recording_id']
        if rid not in metadata:
            continue
        observations[rid] = {'source': 'audio_measurement',
            'fits': {vibe: {'score': float(value), 'support': 1.} for vibe, value in zip(VIBES, scores, strict=True)}}
        replaced.append(rid)
    return metadata, observations, {'recomputed_audio_recordings': replaced,
        'retained_original_sources': dict(Counter(row['source'] for row in observations.values())),
        'propagated_recordings_added_as_votes': 0}


def consumer_adapter(original, library, metadata, observations):
    from yt_playlist.rec import home_vibe, musical_profiles, vibe_prediction
    ids = {row['identity_key']: row['id'] for row in library['recordings']}
    statistics = cohorts(metadata, observations)
    calls = {'pages': 0, 'recording_reads': 0, 'adapted_reads': 0, 'missing_frozen_identity_reads': 0,
             'original_profile_load_wall_s': 0., 'profile_copy_wall_s': 0.,
             'candidate_profile_update_wall_s': 0.}

    def load(store, keys):
        started = time.monotonic()
        try:
            originals = original(store, keys)
        finally:
            calls['original_profile_load_wall_s'] += time.monotonic() - started
        started = time.monotonic()
        try:
            profiles = deepcopy(originals)
        finally:
            calls['profile_copy_wall_s'] += time.monotonic() - started
        calls['pages'] += 1
        calls['recording_reads'] += len(profiles)
        started = time.monotonic()
        try:
            for key, profile in profiles.items():
                rid = ids.get(key)
                if rid not in metadata:
                    calls['missing_frozen_identity_reads'] += 1
                    continue
                own = observations.get(rid)
                candidates = []
                for group in vibe_prediction.groups(**metadata[rid]):
                    stats = statistics.get(group, {})
                    # Resolve own evidence separately; it cannot vote for itself.
                    if own:
                        stats = vibe_prediction.adjust(stats, own, -1)
                    candidates.append((group, stats))
                fallback = {vibe: home_vibe.heuristic_affinity(profile, vibe) for vibe in VIBES}
                profile['vibes'] = vibe_prediction.resolve(fallback, own, candidates)
                profile['digest'] = musical_profiles.digest(profile)
                calls['adapted_reads'] += 1
        finally:
            calls['candidate_profile_update_wall_s'] += time.monotonic() - started
        return profiles
    return load, calls


def paired(database, capture_seconds, publication_seconds):
    from tests.bench_svd_background import _backup_library
    from experiments.investigations.bench_musical_character import test_saved_catalog_musical_character_audit
    from yt_playlist.rec import audio_vibe_model, musical_character
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from yt_playlist.util import genre_map
    library, samples = frozen_inputs()
    if capture_seconds != 0:
        raise ValueError('Paired replay requires --seconds 0 so every copy retains identical prepared state')
    candidates = frozen_models()
    checkpoint = json.loads(candidates['checkpoint'].read_text())
    acoustic, excluded = acoustic_inputs(library, samples, checkpoint['embedding_signature'])
    fixed_metadata, _ = current_inputs(library, {'provider_measurement', 'audio_measurement'})
    genre_counts = Counter(genre_map.family(row['genre']) for row in fixed_metadata.values() if row['genre'])
    fixed_genres = [genre for genre, _count in genre_counts.most_common(2) if genre]
    # Fixed comparisons, chosen before reading any reserved-cohort result.
    choices = [('checkpoint', 'prediction'), ('core_consensus', 'selected_candidate'),
               ('consensus_expanded', 'selected_candidate'),
               ('expanded_rankings', 'selected_candidate'),
               ('expanded_semantics', 'equal_all_activity_support')]
    paths = {name: candidates[name] for name, _ in choices}
    if any(not path.exists() for path in paths.values()):
        raise ValueError('Finish all fixed replay candidates first')
    before = runtime_hashes()
    target = WORK / ('paired-' + stamp().replace(':', '').replace('+', '_'))
    target.mkdir(parents=True)
    frozen_database = target / 'source.db'
    backup = _backup_library(Path(database), frozen_database)
    plan = {'created_at': stamp(), 'database': str(Path(database).resolve()),
        **runtime_identity(),
        'backup': backup, 'frozen_database_sha256': digest(frozen_database),
        'models': {name: {'path': str(path), 'sha256': digest(path), 'method': method}
            for name, method in choices for path in [paths[name]]},
        'runtime_code_sha256': before, 'adapter_sha256': digest(__file__),
        'input_manifest_sha256': digest(RUN / 'inputs/manifest.json'),
        'complete_compatible_audio_recordings': len(acoustic), 'excluded_incomplete_audio': excluded,
        'capture_seconds_per_copy': 0,
        'publication_limit_s': publication_seconds,
        'extended_publication_diagnostic': publication_seconds > 180,
        'explicit_genre_families': fixed_genres,
        'radio_seed_policy': 'Select one eligible seed using the checkpoint consumer, then hold it fixed across all candidates and dials.',
        'policy': 'Same saved library, fixed comparison candidates, original observations only, one recording vote. Replace only complete compatible captures. Consumers and propagation use production code; model loading uses an explicit process-local research adapter.',
        'limitations': ['No independent human judgment of generated lists.',
            'Saved prepared state is retained; cold preparation is measured separately.',
            'Frozen observations predate the live backup. Missing frozen identities retain the copied serving profile.',
            'Unsupported captures retain their original teacher scores, identified separately.',
            'This bypasses product artifact loading and update invalidation; it cannot establish deployability.']}
    save(target / 'plan.json', plan)
    original = musical_character.load
    seed_offer = RadioCandidates.seed_offer
    fixed_seed = []
    def common_seed(candidate, count=16):
        if count != 1:
            return seed_offer(candidate, count)
        if not fixed_seed:
            fixed_seed.extend(deepcopy(seed_offer(candidate, count)))
        result = deepcopy(fixed_seed)
        profiles = musical_character.load(candidate.store, [row['key'] for row in result])
        for row in result:
            row['musical'] = profiles.get(row['key'])
        candidate.rng.seed(136)
        return result
    reports = {}
    for name, method in choices:
        path = paths[name]
        if digest(path) != plan['models'][name]['sha256']:
            raise ValueError('Replay candidate changed')
        artifact = json.loads(path.read_text())
        metadata, observations, audit = candidate_observations(library, acoustic, artifact, method)
        adapter, calls = consumer_adapter(original, library, metadata, observations)
        output = target / name
        with tempfile.TemporaryDirectory(prefix='private-', dir=target) as temporary, MonkeyPatch.context() as patch:
            random.seed(136)
            np.random.seed(136)
            patch.setenv('TUNECONSOLE_BENCH_DB', str(frozen_database.resolve()))
            patch.setenv('TUNECONSOLE_MUSICAL_REPORT_DIR', str(output.resolve()))
            patch.setenv('TUNECONSOLE_MUSICAL_CAPTURE_SECONDS', str(capture_seconds))
            patch.setenv('TUNECONSOLE_BENCH_NOW', '1793260800')
            patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(candidates['checkpoint']))
            patch.setattr(musical_character, 'load', adapter)
            patch.setattr(RadioCandidates, 'seed_offer', common_seed)
            # Freeze the consumer evaluation clock across all copies.
            patch.setattr('experiments.investigations.bench_musical_character.time.time', lambda: 1793260800.)
            if publication_seconds == 180:
                test_saved_catalog_musical_character_audit(Path(temporary), patch)
            else:
                from .consumer_replay import evaluate
                evaluate(Path(temporary), patch, publication_seconds=publication_seconds,
                         diagnostics={'profile_adapter': calls})
            constraints = consumer_constraints(Path(temporary) / 'musical-library.db', output,
                fixed_genres, common_seed_track=fixed_seed[0] if fixed_seed else None)
        if not calls['adapted_reads']:
            raise ValueError('Consumer replay did not read candidate musical profiles: ' + name)
        reports[name] = {'observation_audit': audit, 'consumer_calls': calls,
                         'constraints': constraints,
                         'report': json.loads((output / 'report.json').read_text())}
        save(target / 'progress.json', {'completed': list(reports), 'active': None})
    unchanged = before == runtime_hashes()
    save(target / 'report.json', {'created_at': stamp(), 'methods': reports, 'complete': unchanged,
        'extended_publication_diagnostic': publication_seconds > 180,
        'publication_limit_s': publication_seconds,
        'original_publication_bound_passed': publication_seconds == 180,
        'fixed_radio_seed': fixed_seed,
        'output_comparisons': compare_outputs(target, [name for name, _ in choices]),
        'runtime_code_unchanged_during_run': unchanged, 'plan_sha256': digest(target / 'plan.json')})
    if not unchanged:
        raise ValueError('Production runtime code changed during paired replay; report is incomplete')
    print(json.dumps({'paired_library_replay': str(target), 'completed': list(reports)}), flush=True)


def consumer_constraints(database, output, genres, *, common_seed_track):
    """Exercise explicit filters with actual saved-library candidates."""
    from yt_playlist.core.activity import cancellable_work
    from yt_playlist.core.store import Store
    from yt_playlist.rec import home_vibe, musical_character, radio_tuning, vibe_pools
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from yt_playlist.rec.radio_genres import GenrePolicy, genre
    from yt_playlist.util import genre_map
    store = Store(database)
    start = time.monotonic()
    mixes, radios = {}, {}
    report = {'mixes': mixes, 'radio': radios, 'violations': []}
    try:
        with cancellable_work(lambda: time.monotonic() - start > 180):
            store.init_schema()
            now = 1793260800.
            surfaces = ['radio', *('vibe:' + preset for preset in home_vibe.PRESETS)]
            def heads():
                return {surface: (head['id'] if (head := store.recommendations.head(surface)) else None)
                        for surface in surfaces}
            report['publication_generations'] = heads()
            report['uses_retained_publication'] = True
            if not getattr(store, '_recommendation_files', False) or any(
                    value is None for value in report['publication_generations'].values()):
                raise ValueError('Restriction checks require all previously published consumer surfaces')
            for preset in home_vibe.PRESETS:
                for family in genres:
                    for restriction in ('include', 'avoid'):
                        filters = {('include_genres' if restriction == 'include' else 'avoid_genres'): [family]}
                        cards = vibe_pools.assemble(store, now, preset, **filters)
                        tracks = [track for card in cards for track in card['tracks']]
                        invalid = [row['key'] for row in tracks if
                            (genre_map.family(row.get('genre')) == family) != (restriction == 'include')]
                        repeated = len(tracks) - len({row['key'] for row in tracks})
                        key = preset + ':' + restriction + ':' + family
                        mixes[key] = {'cards': len(cards), 'tracks': len(tracks), 'invalid_genres': invalid,
                            'duplicate_recordings_across_cards': repeated}
                        if invalid or repeated:
                            report['violations'].append(key)
            seed = deepcopy(common_seed_track)
            if seed and genre(seed):
                seed['musical'] = musical_character.load(store, [seed['key']]).get(seed['key'])
                original = json.loads((output / 'candidate-radio.json').read_text())
                blocked = {row['video_id'] for row in original.get('0.5', [])[1:4]}
                for energy in (0, .5, 1):
                    with closing(RadioCandidates(store, now,
                            {**radio_tuning.DEFAULTS, 'theme': 1, 'energy': energy}, comparison=None)) as candidate:
                        candidate.rng.seed(136)
                        history = [deepcopy(seed)]
                        for _ in range(11):
                            following = candidate.next_track(history, excluded=blocked)
                            if following is None:
                                break
                            history.append(dict(following))
                    policy = GenrePolicy(1, [seed], current=seed)
                    violations = [row['video_id'] for row in history[1:]
                        if not policy.allows(row) or row['video_id'] in blocked]
                    repeated = len(history) - len({row['key'] for row in history})
                    radios[str(energy)] = {'seed_genre': genre(seed), 'tracks': history,
                        'explicit_excluded_video_ids': sorted(blocked), 'invalid_tracks': violations,
                        'duplicate_recordings': repeated}
                    if violations or repeated:
                        report['violations'].append('radio:' + str(energy))
            else:
                report['radio_skip_reason'] = 'The fixed consumer seed has no known genre'
            report['publication_unchanged'] = heads() == report['publication_generations']
            if not report['publication_unchanged']:
                raise ValueError('Restriction checks changed the prepared publication')
    finally:
        store.conn.close()
        report['wall_s'] = time.monotonic() - start
        save(output / 'consumer-constraints.json', report)
    if report['violations']:
        raise ValueError('Consumer restriction violations: ' + repr(report['violations']))
    return {'wall_s': report['wall_s'], 'mix_conditions': len(mixes),
        'nonempty_mix_conditions': sum(row['tracks'] > 0 for row in mixes.values()),
        'radio_conditions': len(radios), 'violations': report['violations'],
        'uses_retained_publication': report['uses_retained_publication'],
        'publication_generations': report['publication_generations'],
        'publication_unchanged': report['publication_unchanged'],
        **({'radio_skip_reason': report['radio_skip_reason']} if 'radio_skip_reason' in report else {})}


def compare_outputs(target, names, *, paths=None):
    from yt_playlist.rec.musical_character import intensity, intensity_evidence, provenance
    def intensity_coverage(tracks):
        evidence = [intensity_evidence(row) for row in tracks]
        traces = [provenance(row) for row in tracks]
        known = [(value, support) for value, support in evidence if value is not None]
        pairs = [abs(a[0] - b[0]) for a, b in zip(evidence, evidence[1:])
                 if a[0] is not None and b[0] is not None]
        return {'known_tracks': len(known), 'unknown_tracks': len(tracks) - len(known),
            'positive_support_tracks': sum(support > 0 for _value, support in known),
            'selected_input_sources': dict(Counter(
                row['intensity_source'] or ('profile_dimension' if row['intensity'] is not None else 'unknown')
                for row in traces)),
            'selected_vibe_inputs': dict(Counter(row['intensity_vibe'] for row in traces
                                                if row['intensity_vibe'] is not None)),
            'mean_known_intensity': float(np.mean([value for value, _support in known])) if known else None,
            'known_adjacent_pairs': len(pairs),
            'mean_known_adjacent_intensity_change': float(np.mean(pairs)) if pairs else None}
    flattened = {}
    for name in names:
        output = paths[name] if paths is not None else target / name
        mixes = json.loads((output / 'candidate-mixes.json').read_text())
        radio = json.loads((output / 'candidate-radio.json').read_text())
        playlists = {'mix:' + vibe + ':' + card['lane']: card['tracks']
                     for vibe, cards in mixes.items() for card in cards}
        playlists.update({'radio:' + dial: tracks for dial, tracks in radio.items()})
        flattened[name] = playlists
    baseline = flattened['checkpoint']
    return {name: {surface: {
        'tracks': len(tracks), 'duplicate_recordings': len(tracks) - len({row['key'] for row in tracks}),
        'checkpoint_membership_overlap': len({row['key'] for row in tracks} & {row['key'] for row in baseline.get(surface, [])}),
        'intensity_coverage': intensity_coverage(tracks),
        'mean_shared_intensity': float(np.mean([intensity(row) for row in tracks])) if tracks else None,
        'mean_adjacent_intensity_change': float(np.mean([abs(intensity(a) - intensity(b))
            for a, b in zip(tracks, tracks[1:])])) if len(tracks) > 1 else None}
        for surface, tracks in playlists.items()} for name, playlists in flattened.items()}


def reset_derived(store):
    from yt_playlist.rec import progressive
    deleted = {}
    # Original samples, provider observations and immutable measurement history
    # remain intact. Their serving pointers and derived profiles start empty.
    with store.conn:
        for table in ('recording_vibes', 'rec_vibe_pending', 'rec_musical_profiles',
                      'rec_musical_source_profiles', 'rec_musical_pending',
                      'prepared_recording_inputs', 'unresolved_preparation_inputs', 'rec_content_pending'):
            deleted[table] = store.conn.execute('DELETE FROM ' + table).rowcount
        deleted['content_embeddings'] = store.conn.execute(
            "DELETE FROM recording_embeddings WHERE space IN ('content_library','content_discovery')").rowcount
        keys = ('rec_content_model', 'rec_content_generation', 'rec_content_genre_revision',
                'rec_content_inputs', 'rec_vibe_version', progressive.BASIS_STATE, progressive.FIT_STATE)
        store.conn.executemany('DELETE FROM settings WHERE key=?', [(key,) for key in keys])
        store.conn.execute('INSERT INTO rec_musical_pending(source_key,revision) '
                           'SELECT source_key,revision FROM rec_musical_sources ORDER BY source_key')
    return deleted


def startup(database, seconds):
    from tests.library_image.fixtures import benchmark_library
    from yt_playlist.core.activity import cancellable_work
    from yt_playlist.core.store import Store
    from yt_playlist.rec import audio_vibe_model, musical_preparation, progressive, vibe_preparation
    before = runtime_hashes()
    target = WORK / ('startup-' + stamp().replace(':', '').replace('+', '_'))
    target.mkdir(parents=True)
    report = {'created_at': stamp(), 'runtime_code_sha256': before, 'script_sha256': digest(__file__),
        **runtime_identity(),
        'limit_s': seconds, 'first_profiles': None, 'first_content_vectors': None,
        'limitations': ['Cold derived content/profiles on a populated library; collaborative history and mode definitions are retained. This does not prove complete fresh-account startup.',
            'The bounded run reports incomplete work rather than treating a timeout as completion.']}
    save(target / 'report.json', report)
    with tempfile.TemporaryDirectory(prefix='private-', dir=target) as temporary, MonkeyPatch.context() as patch:
        path = Path(temporary) / 'library.db'
        path, report['backup'] = benchmark_library(Path(database), path)
        def no_network(*args, **kwargs):
            raise AssertionError('Offline experiment attempted a network connection')
        patch.setattr(socket.socket, 'connect', no_network)
        patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(frozen_models()['checkpoint']))
        store = Store(path)
        try:
            started = time.monotonic()
            store.init_schema()
            report['schema_upgrade_s'] = time.monotonic() - started
            report['cleared'] = reset_derived(store)
            preparation = progressive.Preparation(store)
            report['before'] = preparation.status()
            start, cpu, now = time.monotonic(), time.process_time(), time.time()
            report['progress'] = []
            for step in range(2048):
                if time.monotonic() - start >= seconds:
                    break
                with cancellable_work(lambda: time.monotonic() - start >= seconds + 5):
                    preparation.capture(now + step * 5)
                    if report['first_content_vectors'] is None:
                        preparation.fit(now + step * 5)
                profiles = store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0]
                if profiles and report['first_profiles'] is None:
                    report['first_profiles'] = {'wall_s': time.monotonic() - start, 'profiles': profiles}
                status = preparation.status()
                if (status['library_vectors'] or status['discovered_vectors']) and report['first_content_vectors'] is None:
                    report['first_content_vectors'] = {'wall_s': time.monotonic() - start, **status}
                if step % 32 == 0:
                    report['progress'].append({'step': step, 'wall_s': time.monotonic() - start, 'profiles': profiles, **status})
                    save(target / 'report.json', report)
                if not (store.prepared_content.has_pending() or musical_preparation.due(store) or vibe_preparation.due(store)):
                    break
            report.update(steps=step + 1, wall_s=time.monotonic() - start, cpu_s=time.process_time() - cpu,
                after=preparation.status(), pending={table: store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('rec_musical_pending', 'rec_content_pending', 'rec_vibe_pending')})
            report['complete'] = not any(report['pending'].values())
        finally:
            store.conn.close()
            report['runtime_code_unchanged_during_run'] = before == runtime_hashes()
            if not report['runtime_code_unchanged_during_run']:
                report['complete'] = False
            save(target / 'report.json', report)
    if not report['runtime_code_unchanged_during_run']:
        raise ValueError('Production runtime code changed during cold preparation; report is incomplete')
    print(json.dumps({'cold_preparation': str(target), 'complete': report.get('complete'),
        'first_profiles': report['first_profiles'], 'first_content_vectors': report['first_content_vectors']}), flush=True)


def fresh(seconds):
    """Start with no prepared state, then admit real captured evidence in stages."""
    from yt_playlist.core.activity import cancellable_work
    from yt_playlist.core.store import Store
    from yt_playlist.rec import audio_vibe_model, musical_character, musical_preparation, progressive, vibe_preparation
    library, samples = frozen_inputs()
    paths = frozen_models()
    checkpoint = json.loads(paths['checkpoint'].read_text())
    acoustic, _excluded = acoustic_inputs(library, samples, checkpoint['embedding_signature'])
    records = {row['id']: row for row in library['recordings']}
    selected = {row['recording_id']: set(row['sample_ids']) for row in acoustic}
    inputs = [(metadata, sample) for rid, values in samples.items() if rid in selected
              for metadata, sample in values if metadata['id'] in selected[rid]]
    phases = {}
    before = runtime_hashes()
    target = WORK / ('fresh-' + stamp().replace(':', '').replace('+', '_'))
    target.mkdir(parents=True)
    report = {'created_at': stamp(), 'runtime_code_sha256': before, 'script_sha256': digest(__file__),
        **runtime_identity(),
        'input_manifest_sha256': digest(RUN / 'inputs/manifest.json'),
        'checkpoint_sha256': digest(paths['checkpoint']), 'real_recordings': len(selected),
        'real_sample_ids': sorted(metadata['id'] for metadata, _sample in inputs), 'phases': phases,
        'limitations': ['Bounded real captured-audio cohort, not a complete account import or signed-in startup.',
            'Original title/artist metadata only; provider enrichment and collaborative history are absent.',
            'One explicit empty-artist control and temporary sample-file unavailability are controlled interventions.',
            'Worker admission time advances between slices; measured wall time excludes scheduling idle time.',
            'This verifies the existing checkpoint preparation path, not loading any research candidate.']}
    save(target / 'report.json', report)
    with MonkeyPatch.context() as patch:
        def no_network(*args, **kwargs):
            raise AssertionError('Fresh replay attempted a network connection')
        patch.setattr(socket.socket, 'connect', no_network)
        patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(paths['checkpoint']))
        store = Store(target / 'library.db')
        keys, admission = {}, 0
        control_key = None

        def drain(name):
            nonlocal admission
            started, cpu = time.monotonic(), time.process_time()
            stage = phases[name] = {'complete': False, 'first_profiles_s': None, 'first_vectors_s': None}
            for step in range(2048):
                if time.monotonic() - started >= seconds:
                    break
                with cancellable_work(lambda: time.monotonic() - started >= seconds + 5):
                    preparation.capture(1793260800. + admission * 5)
                    if stage['first_vectors_s'] is None:
                        preparation.fit(1793260800. + admission * 5)
                admission += 1
                count = store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0]
                status = preparation.status()
                if count and stage['first_profiles_s'] is None:
                    stage['first_profiles_s'] = time.monotonic() - started
                if status['library_vectors'] and stage['first_vectors_s'] is None:
                    stage['first_vectors_s'] = time.monotonic() - started
                if not (store.prepared_content.has_pending() or musical_preparation.due(store) or vibe_preparation.due(store)):
                    stage['complete'] = True
                    break
            stage.update(steps=step + 1, wall_s=time.monotonic() - started, cpu_s=time.process_time() - cpu,
                status=preparation.status(), measurements=store.conn.execute('SELECT COUNT(*) FROM measurements').fetchone()[0],
                current_sources=dict(store.conn.execute('SELECT source,COUNT(*) FROM recording_vibes GROUP BY source')),
                profiles=musical_character.load(store, [*keys.values(), control_key]))
            save(target / 'report.json', report)
            if not stage['complete']:
                raise ValueError('Fresh cohort did not drain within its bound: ' + name)

        try:
            store.init_schema()
            preparation = progressive.Preparation(store)
            report['initial_counts'] = {table: store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('recordings', 'sample_attributes', 'measurements', 'predictions', 'recording_embeddings', 'audio_annotations')}
            if any(report['initial_counts'].values()):
                raise ValueError('Fresh replay unexpectedly contains prior state')
            for rid in sorted(selected):
                sample = next(sample for metadata, sample in inputs if metadata['recording_id'] == rid)
                row = records[rid]
                store.tracks.upsert_track(sample['video_id'], row['title'], row['artist'], None, None)
                keys[rid] = store.tracks.identity_key_for_video(sample['video_id'])
            store.tracks.upsert_track('experiment-missing-artist', 'Missing artist control', '', None, None)
            control_key = store.tracks.identity_key_for_video('experiment-missing-artist')
            drain('metadata_only')
            for metadata, sample in inputs:
                if metadata['point'] == 'thirty':
                    store.audio_vibes.save_sample(sample, metadata['captured_at'])
            drain('one_excerpt_per_recording')
            for metadata, sample in inputs:
                if metadata['point'] != 'thirty':
                    store.audio_vibes.save_sample(sample, metadata['captured_at'])
            drain('three_excerpts_per_recording')
            measured_before = {row[0] for row in store.conn.execute('SELECT id FROM measurements')}
            # Only private copied evidence is removed; immutable historical
            # measurement rows remain. A normal collection retry restores it.
            rid = min(selected)
            private = list(store.conn.execute('SELECT id,filename FROM sample_attributes WHERE recording_id=('
                'SELECT id FROM recordings WHERE identity_key=?)', (keys[rid],)))
            saved_bytes = [(Path(row['filename']), Path(row['filename']).read_bytes()) for row in private]
            if len(saved_bytes) != 3:
                raise ValueError('Missing-file control did not resolve one complete recording')
            for path, _raw in saved_bytes:
                path.unlink()
            with store.conn:
                store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?) '
                    'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1', (keys[rid],))
            drain('missing_original_files')
            for metadata, sample in inputs:
                if metadata['recording_id'] == rid:
                    store.audio_vibes.save_sample(sample, metadata['captured_at'])
            drain('collection_retry_restores_files')
            completed = phases['three_excerpts_per_recording']['profiles']
            restored = phases['collection_retry_restores_files']['profiles']
            report['retry_maximum_score_delta'] = max(abs(
                completed[key]['vibes']['fits'][vibe]['score'] - restored[key]['vibes']['fits'][vibe]['score'])
                for key in completed for vibe in VIBES)
            report['missing_file_control_sources'] = {
                vibe: fit['source'] for vibe, fit in phases['missing_original_files']['profiles'][keys[rid]]['vibes']['fits'].items()}
            report['empty_artist_control_sources'] = {
                vibe: fit['source'] for vibe, fit in restored[control_key]['vibes']['fits'].items()}
            report['immutable_measurement_history_preserved'] = measured_before <= {
                row[0] for row in store.conn.execute('SELECT id FROM measurements')}
            report['foreign_key_errors'] = [tuple(row) for row in store.conn.execute('PRAGMA foreign_key_check')]
            report['development_annotations'] = store.conn.execute('SELECT COUNT(*) FROM audio_annotations').fetchone()[0]
            if not report['immutable_measurement_history_preserved'] or report['foreign_key_errors'] or report['development_annotations']:
                raise ValueError('Fresh replay violated history, relational integrity or label isolation')
            if (report['retry_maximum_score_delta'] > 1e-10 or
                    'audio_measurement' in report['missing_file_control_sources'].values() or
                    set(report['empty_artist_control_sources'].values()) != {'heuristic'}):
                raise ValueError('Fresh replay failed missing-file recovery or missing-identity isolation')
            report['complete'] = True
        except BaseException as error:
            report['error'] = repr(error)
            raise
        finally:
            store.conn.close()
            report['runtime_code_unchanged_during_run'] = before == runtime_hashes()
            if not report['runtime_code_unchanged_during_run']:
                report['complete'] = False
            save(target / 'report.json', report)
    if not report['runtime_code_unchanged_during_run']:
        raise ValueError('Production runtime code changed during fresh preparation; report is incomplete')
    print(json.dumps({'fresh_preparation': str(target), 'complete': report['complete'],
        'phases': {name: {key: stage[key] for key in ('complete', 'wall_s', 'first_profiles_s', 'first_vectors_s')}
                   for name, stage in phases.items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'runtime-audit', 'freeze-runtime', 'startup', 'fresh',
                                         'fresh-consumers', 'paired', 'paired-slice', 'verify-paired', 'finish-constraints'])
    parser.add_argument('--database', type=Path, default=Path('/home/rfirmin/.local/share/yt-playlist/state.db'))
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--report', type=Path, help='Existing replay report for the lightweight runtime-audit action')
    parser.add_argument('--resume', type=Path, help='Existing paired-slices campaign; validates provenance before reusing completed candidates')
    parser.add_argument('--publication-seconds', type=float, default=180,
                        help='Paired publication bound; above 180 selects an explicitly separate extended diagnostic (at most 600)')
    parser.add_argument('--independent-execution', action='store_true',
                        help='Compatibility option; research always runs independently of pytest')
    args = parser.parse_args()
    if args.resume and args.action not in ('paired-slice', 'verify-paired', 'finish-constraints'):
        parser.error('--resume requires paired-slice, verify-paired or finish-constraints')
    WORK.mkdir(parents=True, exist_ok=True)
    if args.action == 'freeze-runtime':
        freeze_runtime()
        return
    if args.action == 'inspect':
        inspect()
        return
    if args.action == 'runtime-audit':
        if args.report is None:
            parser.error('runtime-audit requires --report')
        runtime_audit(args.report)
        return
    if args.action == 'verify-paired':
        if args.resume is None:
            parser.error('verify-paired requires --resume')
        from .replay_followups import verify_paired
        verify_paired(args.resume)
        return
    with threadpool_limits(limits=1):
        if args.action == 'finish-constraints':
            if args.resume is None:
                parser.error('finish-constraints requires --resume')
            from .replay_followups import finish_constraints
            finish_constraints(args.resume, independent_execution=True)
        elif args.action == 'fresh-consumers':
            from .replay_followups import fresh_consumers
            fresh_consumers(args.database, seconds=120. if args.seconds is None else args.seconds,
                            independent_execution=True)
        elif args.action == 'fresh':
            fresh(120. if args.seconds is None else args.seconds)
        else:
            seconds = args.seconds if args.seconds is not None else (0. if args.action in ('paired', 'paired-slice') else 120.)
            if args.action == 'startup':
                startup(args.database, seconds)
            else:
                if not 180 <= args.publication_seconds <= 600:
                    parser.error('--publication-seconds must be between 180 and 600')
                if args.action == 'paired-slice':
                    if seconds != 0:
                        parser.error('paired-slice requires zero preparation seconds')
                    from .paired_campaign import run
                    run(args.database, publication_seconds=args.publication_seconds, resume=args.resume,
                        independent_execution=True)
                else:
                    paired(args.database, seconds, args.publication_seconds)


if __name__ == '__main__':
    main()
