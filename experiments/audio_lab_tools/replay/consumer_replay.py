"""Actual library consumers with an explicit extended publication diagnostic.

The original 180-second benchmark failure remains separate evidence. This driver
uses unchanged production publication and consumers with a larger recorded outer
bound; finishing here cannot establish that the original bound passed.
"""
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import resource
import socket
import statistics
import sys
import time

from experiments.investigations.bench_musical_character import _catalog, _lookup_evidence, _vector_digest
from tests.bench_svd_background import _saved_library_source
from tests.library_image.fixtures import benchmark_library
from tests.musical_evaluation_support import coverage, holdouts, listening_packet
from yt_playlist.core.activity import cancellable_work
from yt_playlist.core.store import Store
from yt_playlist.rec import home_vibe, mix_tuning, mode_surfaces, musical_profiles, radio_tuning, vibe_pools
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections

from ..support.external_experiment import digest


def evaluate(temporary, patch, *, publication_seconds, diagnostics=None, warm_samples=20):
    if not 180 < publication_seconds <= 600:
        raise ValueError('Extended publication diagnostic requires a bound above 180 and at most 600 seconds')
    if warm_samples not in (0, 20):
        raise ValueError('Use the fixed 20-sample protocol or an explicit no-warm continuation')
    output = Path(os.environ['TUNECONSOLE_MUSICAL_REPORT_DIR'])
    output.mkdir(mode=0o700)
    destination = temporary / 'musical-library.db'
    destination, report = benchmark_library(_saved_library_source(), destination)
    destination.chmod(0o600)
    def no_network(*args, **kwargs):
        raise AssertionError('Consumer diagnostic attempted a network connection')
    patch.setattr(socket.socket, 'connect', no_network)
    store = Store(destination)
    now = float(os.environ['TUNECONSOLE_BENCH_NOW'])
    report.update(schema=1, profile_version=musical_profiles.VERSION, vibe_version=home_vibe.VERSION,
        script_sha256=digest(__file__), at=now, measurements={}, human_judgments=0,
        original_publication_limit_s=180, extended_publication_limit_s=publication_seconds,
        original_publication_bound_passed=False, cf_fit=False, network_requests=0,
        warm_samples_requested=warm_samples,
        limitations=['Explicit extended publication diagnostic after the original bound failed.',
                     'Timings include the process-local candidate adapter and payload read guards; these are not pure production latency measurements.',
                     'No human playlist judgments or product artifact-loader acceptance.'])
    def save(name, value):
        path = output / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
    def measure(name, target, *, seconds=180):
        before = {key: dict(values) for key, values in (diagnostics or {}).items()}
        wall, cpu = time.monotonic(), time.process_time()
        try:
            with cancellable_work(lambda: time.monotonic() - wall > seconds):
                return target()
        finally:
            report['measurements'][name] = {'wall_s': time.monotonic() - wall,
                'cpu_s': time.process_time() - cpu, 'limit_s': seconds}
            if before:
                report['measurements'][name]['diagnostics'] = {
                    group: {key: value - before[group][key] for key, value in values.items()}
                    for group, values in diagnostics.items()}
            save('report.json', report)
    def repeated(name, target):
        if not warm_samples:
            return
        samples = [measure(name + ':' + str(index), lambda index=index: target(index), seconds=30)
                   for index in range(warm_samples)]
        values = [report['measurements'].pop(name + ':' + str(index)) for index in range(warm_samples)]
        report['measurements'][name] = {'samples': len(samples), 'cache': 'warm_process',
            **{metric: {'sum': sum(row[metric] for row in values),
                        'median': statistics.median(row[metric] for row in values),
                        'p95': sorted(row[metric] for row in values)[18],
                        'max': max(row[metric] for row in values)} for metric in ('wall_s', 'cpu_s')}}
        if diagnostics:
            report['measurements'][name]['diagnostic_totals'] = {
                group: {key: sum(row['diagnostics'][group][key] for row in values) for key in counters}
                for group, counters in diagnostics.items()}
    try:
        measure('schema_upgrade', store.init_schema)
        store.settings.set_setting('rec_selection_paused', '1')
        mix_tuning.reset(store)
        cf_before = _vector_digest(store)
        report['capture_steps'] = 0
        report['remaining_preparation'] = {table: store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            for table in ('rec_content_pending', 'rec_musical_pending', 'rec_vibe_pending')}
        rows = measure('catalog_read', lambda: _catalog(store))
        profiles = {key: json.loads(payload) for key, payload in store.conn.execute(
            'SELECT identity_key,payload FROM rec_musical_profiles')}
        report.update(catalog_recordings=len(rows), profiles=len(profiles),
                      coverage=coverage(profiles, [row['key'] for row in rows]),
                      lookup_evidence=_lookup_evidence(store))
        counts = Counter(row.get('genre') for row in rows if row.get('genre'))
        held = [genre for genre, count in sorted(counts.items())
                if count >= 10 and int(musical_profiles.digest(genre)[:8], 16) % 5 == 0]
        splits = holdouts(rows, held)
        packet, manifest = listening_packet(rows, profiles, splits)
        save('listening-packet.json', packet)
        save('sampling-manifest.json', {'held_genres': held, 'splits': splits, 'items': manifest})
        report['listening_packet'] = {'unlabelled_tracks': len(packet['tracks']),
                                      'unlabelled_intensity_pairs': len(packet['intensity_pairs'])}
        stage = store.recommendations.stage
        def record_stage(plan, build):
            report['publication_plan'] = {'slots': len(plan.slots), 'outputs': len(plan.outputs),
                'candidate_rows_across_slots': sum(len(slot.pool.candidates) for slot in plan.slots),
                'output_bytes': sum(len(output.encoded) for output in plan.outputs)}
            save('report.json', report)
            return stage(plan, build)
        patch.setattr(store.recommendations, 'stage', record_stage)
        bundles = measure('worker_bundles', lambda: mode_surfaces.prepare_bundles(store, now),
                          seconds=publication_seconds)
        if not bundles or bundles['_vibes']['profile_version'] != musical_profiles.VERSION:
            raise ValueError('Consumer diagnostic did not produce current musical pools')
        report['diagnostic_publication_wall_s'] = report['measurements']['worker_bundles']['wall_s']
        mixes = {}
        for preset in home_vibe.PRESETS:
            mixes[preset] = measure('mix_assembly:' + preset, lambda preset=preset:
                vibe_pools.assemble(store, now, preset, bundles=bundles))
            repeated('mix_assembly_warm:' + preset, lambda _index, preset=preset:
                vibe_pools.assemble(store, now, preset, bundles=bundles))
        save('candidate-mixes.json', mixes)
        report['mix_track_counts'] = {preset: {card['lane']: len(card['tracks']) for card in cards}
                                     for preset, cards in mixes.items()}
        radio, seed = {}, None
        for intensity in (0, .5, 1):
            def episode():
                nonlocal seed
                with closing(RadioCandidates(store, now,
                        {**radio_tuning.DEFAULTS, 'energy': intensity}, comparison=None)) as candidates:
                    candidates.rng.seed(136)
                    if seed is None:
                        seed = next(iter(candidates.seed_offer(1)), None)
                    if not seed:
                        return []
                    history = [dict(seed)]
                    for _ in range(23):
                        following = candidates.next_track(history)
                        if following is None:
                            break
                        history.append(dict(following))
                    return history
            radio[str(intensity)] = measure('radio_episode:' + str(intensity), episode)
        save('candidate-radio.json', radio)
        report['radio_track_counts'] = {dial: len(tracks) for dial, tracks in radio.items()}
        if seed:
            with closing(RadioCandidates(store, now, radio_tuning.DEFAULTS, comparison=None)) as base:
                history = next((tracks[:6] for tracks in radio.values() if tracks), [seed])
                def retune(index):
                    with closing(RadioCandidates(store, now,
                            {**radio_tuning.DEFAULTS, 'energy': index % 2},
                            now_source=base, comparison=None)) as candidate:
                        candidate.rng.seed(136)
                        RadioDirections(candidate, seed).update(history, [], 5, force=True)
                repeated('radio_retune_candidates_and_four_directions', retune)
        if cf_before != _vector_digest(store):
            raise ValueError('Consumer diagnostic changed collaborative vectors')
        report['status'] = 'awaiting_matching_review_and_listening'
        report['consumer_outputs_complete'] = True
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        store.conn.close()
        scale = 1 if sys.platform == 'darwin' else 1024
        report['process_lifetime_peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
        save('report.json', report)
