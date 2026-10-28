"""Opt-in #405 metadata/performance audit and unlabelled listening packet.

uv run pytest -s tests/bench_musical_character.py --test-lock-wait=60
TUNECONSOLE_BENCH_DB selects the source, opened read-only. All migrations,
preparation, settings changes and artifacts stay in pytest's private copy.
TUNECONSOLE_MUSICAL_REPORT_DIR selects a NEW private artifact directory.
TUNECONSOLE_MUSICAL_CAPTURE_SECONDS bounds progressive capture (default 60).
No network calls, audio downloads, real exposures or promotion occur here.
"""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import resource
import socket
import statistics
import sys
import tempfile
import time

from tests.bench_svd_background import _backup_library, _saved_library_source
from tests.musical_evaluation_support import coverage, holdouts, listening_packet
from yt_playlist.core.activity import cancellable_work, check_cancellable_work
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import (home_vibe, mix_tuning, mode_surfaces,
                             musical_preparation, musical_profiles, radio_tuning, vibe_pools)
from yt_playlist.rec.progressive import Preparation
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections


def _catalog(store):
    artists, albums = defaultdict(set), defaultdict(set)
    for key, artist in store.conn.execute('SELECT r.identity_key,COALESCE(a.artist_id,t.artist_id) '
            'FROM track_artists t JOIN catalog_entries e ON e.id=t.entry_id '
            'JOIN recordings r ON r.id=e.recording_id LEFT JOIN artist_redirects a ON a.retired_id=t.artist_id'):
        artists[key].add(artist)
    for key, album in store.conn.execute('SELECT r.identity_key,t.album_id FROM album_tracks t '
            'LEFT JOIN catalog_entries e ON e.id=t.entry_id '
            'JOIN recordings r ON r.id=COALESCE(e.recording_id,t.recording_id)'):
        albums[key].add(album)
    rows = {}
    for row in store.conn.execute('SELECT identity_key AS key,video_id,title,artist,album,genre,mb_year AS year,'
            'bpm,energy,instrumental,duration_s FROM catalog_tracks '
            'WHERE video_id IS NOT NULL AND available IS NOT 0 ORDER BY id'):
        item = dict(row)
        item['artist_ids'], item['album_ids'] = sorted(artists[item['key']]), sorted(albums[item['key']])
        rows.setdefault(item['key'], item)
    return list(rows.values())


def _lookup_evidence(store):
    counts = Counter()
    for provider, raw in store.conn.execute('SELECT provider,provenance_json FROM metadata_findings '
                                           'WHERE provenance_json IS NOT NULL'):
        value = json.loads(raw)
        counts['observations'] += 1
        counts['provider:' + provider] += 1
        counts['scope:' + str(value.get('scope'))] += 1
        counts['lookup:' + str(value.get('match_evidence', {}).get('method'))] += 1
        counts['provider_version_present'] += value.get('provider_version') is not None
        counts['model_version_present'] += value.get('model_version') is not None
    return {'counts': dict(counts), 'manually_verified_recording_matches': None,
            'note': 'Retained lookup evidence is not an independent recording/version verification.'}


def _vector_digest(store):
    digest = hashlib.sha256()
    for key, vector in store.conn.execute('SELECT identity_key,vec FROM rec_vectors ORDER BY identity_key'):
        digest.update(musical_profiles.canonical([key, vector.hex()]).encode())
    return digest.hexdigest()


def test_saved_catalog_musical_character_audit(tmp_path, monkeypatch):
    requested_output = os.environ.get('TUNECONSOLE_MUSICAL_REPORT_DIR')
    output = Path(requested_output) if requested_output else Path(tempfile.mkdtemp(prefix='tuneconsole-musical-audit-'))
    if requested_output:
        output.mkdir(mode=0o700)  # refuse to overwrite or combine evaluation runs
    output.chmod(0o700)
    destination = tmp_path / 'musical-library.db'
    report = _backup_library(_saved_library_source(), destination)
    destination.chmod(0o600)
    def no_network(*args, **kwargs):
        raise AssertionError('The offline musical audit attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    limit_native_threads()
    store = Store(destination)
    now = time.time()
    report.update(schema=1, profile_version=musical_profiles.VERSION, vibe_version=home_vibe.VERSION,
                  at=now, measurements={}, human_judgments=0, promotion='not_evaluated')
    def save(name, value):
        path = output / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
    def measure(name, target, *, seconds=180):
        wall, cpu = time.monotonic(), time.process_time()
        try:
            with cancellable_work(lambda: time.monotonic() - wall > seconds):
                return target()
        finally:
            report['measurements'][name] = {'wall_s': time.monotonic() - wall,
                                           'cpu_s': time.process_time() - cpu}
    def repeated(name, target, count=20):
        samples = []
        for index in range(count):
            wall, cpu = time.monotonic(), time.process_time()
            with cancellable_work(lambda: time.monotonic() - wall > 30):
                target(index)
            samples.append({'wall_s': time.monotonic() - wall, 'cpu_s': time.process_time() - cpu})
        report['measurements'][name] = {'samples': count, 'cache': 'warm_process',
            **{metric: {'median': statistics.median(row[metric] for row in samples),
                        'p95': sorted(row[metric] for row in samples)[int(.95 * (count - 1))],
                        'max': max(row[metric] for row in samples)} for metric in ('wall_s', 'cpu_s')}}
    try:
        measure('schema_upgrade', store.init_schema)
        # Preserve exactly what was saved, before changing controls/preparation
        # in the private copy. This is not an old-code paired incumbent replay.
        saved = {surface: store.rec_proposals.get_proposals(surface)
                 for surface in ('mode_bundles', 'home_prepared', 'fresh_pool')}
        save('saved-publications.json', {'kind': 'saved_outputs_not_paired_incumbent_replay', 'surfaces': saved})
        report['saved_publication_digests'] = {name: musical_profiles.digest(value) for name, value in saved.items()}
        store.settings.set_setting('rec_selection_paused', '1')
        mix_tuning.reset(store)
        cf_before = _vector_digest(store)
        capture_seconds = float(os.environ.get('TUNECONSOLE_MUSICAL_CAPTURE_SECONDS', '60'))
        def prepare():
            preparation, steps = Preparation(store), 0
            deadline = time.monotonic() + capture_seconds
            next_progress = time.monotonic() + 30
            while store.prepared_content.has_pending() or musical_preparation.due(store):
                check_cancellable_work()
                if time.monotonic() >= deadline:
                    break
                preparation.capture(now)
                steps += 1
                if time.monotonic() >= next_progress:
                    print(json.dumps({'capture_progress': steps, 'remaining': {
                        table: store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                        for table in ('rec_musical_pending', 'rec_content_pending')}}), flush=True)
                    next_progress = time.monotonic() + 30
            return steps
        report['capture_steps'] = measure('progressive_profile_capture', prepare, seconds=capture_seconds + 5)
        report['remaining_preparation'] = {table: store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            for table in ('rec_content_pending', 'rec_musical_pending')}
        report['preparation_complete'] = not (store.prepared_content.has_pending() or musical_preparation.due(store))
        report['profile_versions'] = dict(store.conn.execute(
            'SELECT version,COUNT(*) FROM rec_musical_profiles GROUP BY version'))
        rows = measure('catalog_read', lambda: _catalog(store))
        profiles = {key: json.loads(payload) for key, payload in store.conn.execute(
            'SELECT identity_key,payload FROM rec_musical_profiles')}
        report['catalog_recordings'] = len(rows)
        report['profiles'] = len(profiles)
        report['coverage'] = coverage(profiles, [row['key'] for row in rows])
        playable = {row['key'] for row in rows}
        library = store.rec_query.library_keys() & playable
        discovery = {row[0] for row in store.conn.execute('SELECT identity_key FROM discovered_tracks')} & playable
        report['coverage_by_membership'] = {
            name: {'recordings': len(keys), 'coverage': coverage(profiles, keys)}
            for name, keys in (('library', library), ('discovery', discovery),
                               ('discovery_unowned', discovery - library))}
        report['coverage_by_membership_note'] = 'Library and discovery can overlap; catalog coverage is not recommendation eligibility or verified-match coverage.'
        report['lookup_evidence'] = _lookup_evidence(store)
        # Freeze genre holdouts before listening. Entire connected artist/album
        # groups containing these genres are held out, including collaborations.
        counts = Counter(row.get('genre') for row in rows if row.get('genre'))
        requested = os.environ.get('TUNECONSOLE_MUSICAL_HOLDOUT_GENRES')
        held_genres = json.loads(requested) if requested else [genre for genre, count in sorted(counts.items())
            if count >= 10 and int(musical_profiles.digest(genre)[:8], 16) % 5 == 0]
        splits = holdouts(rows, held_genres)
        packet, manifest = listening_packet(rows, profiles, splits)
        save('listening-packet.json', packet)
        save('sampling-manifest.json', {'held_genres': held_genres, 'splits': splits,
            'items': manifest, 'identity_review': 'Catalog artist/release groups require manual alias/version review.'})
        report['holdouts'] = {'genres': held_genres, 'recordings': dict(Counter(
            row['split'] for row in splits.values())), 'groups': len({row['group'] for row in splits.values()})}
        report['listening_packet'] = {'unlabelled_tracks': len(packet['tracks']),
                                      'unlabelled_intensity_pairs': len(packet['intensity_pairs'])}
        bundles = measure('worker_bundles', lambda: mode_surfaces.prepare_bundles(store, now))
        assert bundles, 'No prepared pools on this snapshot'
        assert bundles['_vibes']['profile_version'] == musical_profiles.VERSION
        mixes = {}
        for preset in home_vibe.PRESETS:
            mixes[preset] = measure('mix_assembly:' + preset, lambda preset=preset:
                vibe_pools.assemble(store, now, preset, bundles=bundles))
            repeated('mix_assembly_warm:' + preset, lambda _index, preset=preset:
                vibe_pools.assemble(store, now, preset, bundles=bundles))
        save('candidate-mixes.json', mixes)
        report['mix_track_counts'] = {preset: {card['lane']: len(card['tracks']) for card in cards}
                                      for preset, cards in mixes.items()}
        radio, common_seed = {}, None
        for intensity in (0, .5, 1):
            def episode():
                nonlocal common_seed
                candidates = RadioCandidates(store, now, {**radio_tuning.DEFAULTS, 'energy': intensity}, comparison=None)
                candidates.rng.seed(136)
                if common_seed is None:
                    common_seed = next(iter(candidates.seed_offer(1)), None)
                if not common_seed:
                    return []
                history = [dict(common_seed)]
                for _ in range(23):
                    following = candidates.next_track(history)
                    if following is None:
                        break
                    history.append(dict(following))
                return history
            radio[str(intensity)] = measure('radio_episode:' + str(intensity), episode)
        save('candidate-radio.json', radio)
        report['radio_track_counts'] = {dial: len(tracks) for dial, tracks in radio.items()}
        if common_seed:
            base = RadioCandidates(store, now, radio_tuning.DEFAULTS, comparison=None)
            history = next((tracks[:6] for tracks in radio.values() if tracks), [common_seed])
            def retune(index):
                candidate = RadioCandidates(store, now,
                    {**radio_tuning.DEFAULTS, 'energy': index % 2}, now_source=base, comparison=None)
                candidate.rng.seed(136)
                directions = RadioDirections(candidate, common_seed)
                directions.update(history, [], 5, force=True)
            repeated('radio_retune_candidates_and_four_directions', retune)
            report['retune_measurement_scope'] = 'Candidate construction and four direction plans; excludes HTTP, session persistence and remote playlist writes.'
        assert cf_before == _vector_digest(store)
        report['cf_fit'] = False
        report['network_requests'] = 0
        scale = 1 if sys.platform == 'darwin' else 1024
        report['process_peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
        report['status'] = 'awaiting_matching_review_and_listening'
    finally:
        store.conn.close()
        save('report.json', report)
        print(json.dumps({'report': str(output / 'report.json'), **report}), flush=True)
