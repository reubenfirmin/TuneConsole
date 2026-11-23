"""Opt-in profiling of complete Apply on a disposable actual-library copy."""
import cProfile
import ast
from collections import Counter
from contextlib import nullcontext
from functools import wraps
from threading import RLock
import hashlib
import json
from pathlib import Path
import pstats
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.schema.actual_library import copy_readonly_library
from tests.library_image.fixtures import report_file
from yt_playlist.core import db_space
from yt_playlist.core.store import Store
from yt_playlist.rec import measurement, mode_surfaces, musical_character
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.repos import policies
from yt_playlist.repos.radio_catalog import RadioCatalogRepo
from yt_playlist.web.radio_listening import RadioListening
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_state import Phase, Session
from yt_playlist.web.routes import radio_session


def test_prepared_chill_fill_actual_library(request, tmp_path, monkeypatch):
    source = request.config.getoption('--schema-source-db')
    output = request.config.getoption('--radio-tuning-report')
    if not source or not output:
        pytest.skip('Supply --schema-source-db and --radio-tuning-report for this opt-in profile')
    report = {'python': sys.version, 'rank_reads': []}
    database = copy_readonly_library(Path(source).resolve(strict=True), tmp_path / 'radio-fill.db',
                                    monkeypatch, report)
    at = report.get('managed_library', {}).get('created_at', time.time())
    store = Store(database)
    candidates = None
    try:
        store.init_schema()

        def check_private_space(conn, growth=0):
            assert Path(conn.execute('PRAGMA database_list').fetchone()[2]) == database
            db_space.require_space(database.parent, growth, reserve=96 * 1024**2)

        with monkeypatch.context() as prepare:
            prepare.setattr(policies, 'require_database_space', check_private_space)
            mode_surfaces.prepare_bundles(store, at)
        read_ranks = RadioCatalogRepo.tuning_ranks

        def timed_ranks(repo, *args, **kwargs):
            start = time.perf_counter()
            rows = read_ranks(repo, *args, **kwargs)
            report['rank_reads'].append({
                'seconds': time.perf_counter() - start, 'rows': len(rows),
                'includes_offers': kwargs.get('fresh_surface') is not None,
            })
            return rows

        monkeypatch.setattr(RadioCatalogRepo, 'tuning_ranks', timed_ranks)
        candidates = RadioCandidates(store, at)
        group = max(candidates.cluster_members, key=candidates.cluster_members.get)
        seed = candidates._initial_track_group_pool(group)[0]
        session = Session(None, candidates, store.accounts.get_master_account().id, 'Private On Air',
                          phase=Phase.PLAYING, tracks=[seed], selected=[seed], heard=[seed],
                          position=0, furthest=0, directions=RadioDirections(candidates, seed))
        controller = RadioListening(store, None, None, RLock(), lambda: at)
        tuned = candidates.for_tuning({**candidates.tuning, 'energy': 0.})
        start = time.perf_counter()
        assert controller.tune(session, tuned)
        report['apply_seconds'] = time.perf_counter() - start
        assert session.directions.filling
        heads = [slot['tracks'][0]['video_id'] for slot in session.directions.slots]
        assert len(set(heads)) == 4
        start = time.perf_counter()
        controller.fill_tuned_streams(session, cancelled=lambda: False)
        report['fill_seconds'] = time.perf_counter() - start
        assert not session.directions.filling
        report['stream_lengths'] = [len(slot['tracks']) for slot in session.directions.slots]
        assert report['stream_lengths'] == [6, 6, 6, 6]
        assert heads == [slot['tracks'][0]['video_id'] for slot in session.directions.slots]
        assert session.tracks == session.heard == [seed] and session.position == 0
        assert any(read['includes_offers'] and read['rows'] for read in report['rank_reads'])
    finally:
        if candidates is not None:
            candidates.close()
        store.conn.close()
        report_file(output).write_text(json.dumps(report, indent=2) + '\n')


def test_actual_library_tuning(request, tmp_path, monkeypatch):
    source = request.config.getoption('--schema-source-db')
    rotation = request.config.getoption('--radio-rotation-report')
    output = rotation or request.config.getoption('--radio-tuning-report')
    if not source or not output:
        pytest.skip('Supply --schema-source-db and --radio-tuning-report for this opt-in profile')
    report_path = report_file(output).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'python': sys.version, 'source': source, 'source_access': 'read-only copy',
              'provider': 'forbidden; fresh playback presence controlled', 'sources': {}}
    for name in ('rec/radio_candidates.py', 'rec/radio_rotation.py', 'rec/stored_radio.py',
                 'rec/radio_directions.py', 'repos/radio_catalog.py',
                 'repos/recommendation_publications.py', 'controllers/recommendation_publications.py',
                 'core/db_reads.py', 'core/store.py',
                 'core/listening_views.py', 'web/radio_listening.py',
                 'web/radio_session.py', 'web/radio_launch.py', 'web/radio_state.py', 'web/static/radio.js'):
        report['sources'][name] = hashlib.sha256((Path('src/yt_playlist') / name).read_bytes()).hexdigest()
    destination = tmp_path / 'radio-tuning.db'
    started = time.perf_counter()
    destination = copy_readonly_library(Path(source).resolve(strict=True), destination, monkeypatch, report)
    report['copy_seconds'] = time.perf_counter() - started
    store = Store(destination)
    # The managed baseline can predate bounded schema changes in the checkout.
    # Startup runs only against this disposable workspace, before timing Apply.
    store.init_schema()
    try:
        saved, _ = store.radio_checkpoints.load()
        report['checkpoint_sha256'] = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        report['dataset'] = {name: store.conn.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                             for name in ('recordings', 'catalog_entries', 'play_events', 'listening_observations')}
        if rotation:
            _rotation(store, report, report_path, monkeypatch)
            report['phase'] = 'complete'
            return
        baseline = request.config.getoption('--radio-tuning-baseline')
        if baseline:
            report['baseline_revision'] = baseline
        captured_now = time.time()
        variants = ('baseline', 'optimized') if baseline else ('optimized',)
        representative = {'comfort': .17, 'theme': .5, 'energy': .31}
        cases = [('comfort_energy', representative)]
        if not baseline:
            cases.insert(0, ('restored', representative))
            changed = {'comfort': .17, 'theme': .9, 'energy': .31}
            for axes in (('comfort',), ('theme',), ('energy',), ('comfort', 'theme'),
                         ('theme', 'energy'), ('comfort', 'theme', 'energy')):
                cases.append(('_'.join(axes), {axis: value if axis in axes else .5
                                             for axis, value in changed.items()}))
            cases.extend([
                ('eclectic_full_on', {'comfort': .17, 'theme': 0., 'energy': 1.}),
                ('themed_chill', {'comfort': .17, 'theme': 1., 'energy': 0.}),
            ])
        for case, values, profiled in [(name, values, False) for name, values in cases] + [
                ('comfort_energy', representative, True)]:
            for variant in variants:
                # Every measurement starts with the exact same checkpoint,
                # clock, library and random seed. Only private output changes.
                _, revision = store.radio_checkpoints.load()
                store.radio_checkpoints.save(json.dumps(saved), expected=revision)
                name = variant + ('_profile' if profiled else '')
                if not baseline and case != 'comfort_energy':
                    name += '_' + case
                report[name] = {'timings': {}, 'profiled': profiled}
                report['phase'] = name
                report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
                with monkeypatch.context() as measured:
                    if variant == 'baseline':
                        _baseline(measured, baseline, report)
                    _measure(store, report[name], measured,
                             report_path.with_name(report_path.stem + '-' + name + '.json'),
                             captured_now, values, profiled=profiled, warm=case != 'restored')
                if baseline and variant == 'optimized':
                    counterpart = 'baseline' + ('_profile' if profiled else '')
                    report[name]['matching_first_songs'] = sum(
                        old == new for old, new in zip(report[counterpart]['directions'], report[name]['directions'], strict=True))
        report['phase'] = 'complete'
    finally:
        report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
        store.conn.close()


def _rotation(store, report, destination, monkeypatch):
    """Real catalog/prepared streams; controlled starts affect only this snapshot."""
    owned = store.rec_query.library_keys()
    heard = {row[0] for row in store.conn.execute('SELECT r.identity_key FROM recordings r WHERE '
        'EXISTS(SELECT 1 FROM play_events p WHERE p.recording_id=r.id) OR '
        'EXISTS(SELECT 1 FROM listening_observations o WHERE o.recording_id=r.id) UNION '
        "SELECT identity_key FROM rec_response_events WHERE kind='start'")}
    report['rotation_dataset'] = dict(owned=len(owned), unplayed_owned=len(owned - heard), heard=len(heard))
    report['provider'] = 'forbidden; starts and visible offers simulated in private snapshot'
    report['rotation_policy'] = 'Midpoint: equal familiar / unplayed library / unheard unowned, within musical eligibility'
    report['sessions'] = []
    now, all_picks = time.time(), set()
    work = Counter()
    score = RadioCandidates._score
    page = RecommendationPublications.candidate_page
    facts_owner = type(store.radio_catalog)
    facts = facts_owner.rotation_facts

    def counted_score(*args, **kwargs):
        work['scored'] += 1
        return score(*args, **kwargs)

    def counted_page(*args, **kwargs):
        result = page(*args, **kwargs)
        work['prepared_pages'] += 1
        work['prepared_rows'] += len(result['tracks'])
        return result

    def counted_facts(owner, keys):
        keys = list(keys)
        work['rotation_keys'] += len(keys)
        return facts(owner, keys)

    monkeypatch.setattr(RadioCandidates, '_score', counted_score)
    monkeypatch.setattr(RecommendationPublications, 'candidate_page', counted_page)
    monkeypatch.setattr(facts_owner, 'rotation_facts', counted_facts)

    def kind(track):
        return 'familiar' if track['key'] in heard else 'unplayed' if track['key'] in owned else 'new'

    def counts(tracks):
        return dict(Counter(kind(track) for track in tracks))

    for session, length in enumerate((120, 24, 24, 24)):
        current = {'session': session, 'picks': [], 'branches': [], 'timings': {}}
        report['sessions'].append(current)
        started = time.perf_counter()
        candidates = RadioCandidates(store, now + session * 3600)
        candidates.rng.seed(1212 + session)
        current['preparation'] = {'generation': candidates.generation,
            'publication': candidates.prepared.generation if candidates.prepared else None,
            'complete_membership_modes': len(candidates.cluster_members),
            'initial_pool_counts': {mid: counts(pool) for mid, pool in candidates.pools.items()}}
        current['timings']['initialize'] = time.perf_counter() - started
        try:
            started = time.perf_counter()
            seeds = candidates.seed_offer()
            current['timings']['seed_offer'] = time.perf_counter() - started
            assert seeds
            current['seeds'] = counts(seeds)
            measurement.record_recommendations(store, 'radio_seed', seeds, now + session * 3600,
                request_id=f'rotation-{session}-seeds', ranker='radio-seed')
            history = [seeds[0]]
            directions = RadioDirections(candidates, seeds[0])
            directions.update(history, [], 0)
            for branch in directions.slots:
                assert branch and len(branch['tracks']) == 6
                current['branches'].append({'counts': counts(branch['tracks']),
                    'anchor': kind(branch['tracks'][0]), 'keys': [t['key'] for t in branch['tracks']]})
            measurement.record_recommendations(store, 'radio_directions',
                [track for branch in directions.slots for track in branch['tracks']], now + session * 3600,
                request_id=f'rotation-{session}-menu', ranker='radio-session')
            for position in range(length):
                before = work.copy()
                started = time.perf_counter()
                with candidates.prepared.planning_read():
                    track = candidates.next_track(history)
                elapsed = time.perf_counter() - started
                assert track and track['key'] not in {t['key'] for t in history}
                expected = kind(track)
                assert track['_radio_rotation'] == expected
                current['picks'].append(dict(key=track['key'], kind=expected,
                    repeated=track['key'] in all_picks, seconds=elapsed, work=dict(work - before)))
                history.append(track)
                all_picks.add(track['key'])
                store.rec_ledger.record_recommendation_outcomes(f'rotation-{session}-{position}', 'start',
                    [track['key']], now + session * 3600 + position, generated=True)
                heard.add(track['key'])
            current['counts'] = dict(Counter(pick['kind'] for pick in current['picks']))
            current['minimum_24_pick_counts'] = {
                category: min(sum(pick['kind'] == category for pick in current['picks'][offset:offset + 24])
                              for offset in range(length - 23))
                for category in ('familiar', 'unplayed', 'new')}
            current['repeated'] = sum(pick['repeated'] for pick in current['picks'])
            current['timings']['max_pick'] = max(pick['seconds'] for pick in current['picks'])
            current['max_work_per_pick'] = {metric: max(pick['work'].get(metric, 0) for pick in current['picks'])
                                          for metric in work}
            destination.write_text(json.dumps(report, indent=2, default=str) + '\n')
        finally:
            candidates.close()
    report['total_counts'] = dict(sum((Counter(session['counts']) for session in report['sessions']), Counter()))
    report['distinct_picks'] = len(all_picks)
    for session in report['sessions']:
        length = len(session['picks'])
        assert all(.27 * length <= session['counts'].get(kind, 0) <= .4 * length
                   for kind in ('familiar', 'unplayed', 'new')), session['counts']
        assert min(session['minimum_24_pick_counts'].values()) >= 4, session['minimum_24_pick_counts']
        assert session['max_work_per_pick']['scored'] <= 4 * 96
    _launch_rotation(store, report, monkeypatch, now + 4 * 3600)


def _launch_rotation(store, report, monkeypatch, now):
    """Keep creation/readback and generated starts in the real launch workflow."""
    from tests.radio_session_support import radio_controller, play
    from yt_playlist.library import generated_acquisition
    from yt_playlist.web import radio_session as session_module

    create = generated_acquisition.create_generated_playlist
    evidence = report['launch'] = {'issued': [], 'branches': {}}
    with monkeypatch.context() as controlled, radio_controller(controlled, store, []) as (c, remote, _, _, _):
        controlled.setattr(session_module, 'RadioCandidates', RadioCandidates)
        remote.get_song = lambda video: {'videoDetails': {'lengthSeconds': '200'}}
        c.ctx.now_fn = lambda: now

        def issued(store, title, tracks, client, at, account, **options):
            facts = store.radio_catalog.rotation_facts(t['key'] for t in tracks)
            for track in tracks:
                fact = facts[track['key']]
                expected = 'familiar' if fact['heard'] else 'unplayed' if fact['owned'] else 'new'
                assert track['_radio_rotation_slot'] == expected
                evidence['issued'].append({'key': track['key'], 'kind': expected})
            result = create(store, title, tracks, client, at, account, **options)
            evidence['readback'] = [row['videoId'] for row in result['initial_snapshot']['tracks']]
            return result

        controlled.setattr(generated_acquisition, 'create_generated_playlist', issued)
        result = c.start()
        assert result['ok'], result
        assert [t['_radio_rotation_slot'] for t in c.session.tracks] == [t['kind'] for t in evidence['issued']]
        assert [t['video_id'] for t in c.session.tracks] == evidence['readback']
        initial = list(c.session.tracks)

        def listen(video):
            key = store.tracks.identity_key_for_video(video)
            store.rec_ledger.record_recommendation_outcomes(f'launch:{video}', 'start', [key], now, generated=True)
            play(c, video)

        for track in initial:
            listen(track['video_id'])
        while len(c.session.heard) < 7:
            heard = len(c.session.heard)
            listen(remote.ids[-1])
            assert len(c.session.heard) > heard, 'Observed playback did not advance generated Radio'
        state = c.persistence.load()
        evidence['history'] = [{'key': t['key'], 'kind': t.get('_radio_rotation_slot')} for t in state['heard']]
        c.suspend()
        restored = PlaylistRadio(c.ctx)
        controlled.setattr(restored.work, 'start_thread', lambda *args, **kwargs: None)
        try:
            assert restored.join()['active']
            for legacy, theme in ((False, .51), (True, .52)):
                if legacy:
                    for tracks in (restored.session.tracks, restored.session.heard):
                        for track in tracks[:3]:
                            track.pop('_radio_rotation_slot', None)
                            track['_radio_rotation'] = 'familiar'
                result = restored.tune({**restored.session.candidates.tuning, 'theme': theme})
                assert result['ok'], result
                # The fixture suppresses the observer that fills the published heads.
                with restored.operation:
                    restored.listening.fill_tuned_streams(restored.session, cancelled=lambda: False)
                assert not restored.session.directions.filling
                branches = []
                for branch in restored.session.directions.slots:
                    facts = store.radio_catalog.rotation_facts(t['key'] for t in branch['tracks'])
                    counts = Counter('familiar' if facts[t['key']]['heard'] else
                                     'unplayed' if facts[t['key']]['owned'] else 'new' for t in branch['tracks'])
                    branches.append(dict(counts))
                    evidence['branches']['legacy' if legacy else 'preserved'] = branches
                    assert all(counts[kind] >= 1 for kind in ('familiar', 'unplayed', 'new')), counts
        finally:
            restored.suspend()
            restored.work.stop_executor()


def _baseline(monkeypatch, revision, report):
    """Compare original planning algorithms, with the same unbounded worker.

    Keep the corrected controller and reader lifetime in both measurements so
    both complete the requested operation instead of comparing an aborted plan
    with a complete one. No source file or checkout is modified.
    """
    from yt_playlist.rec import radio_candidates, stored_radio
    from yt_playlist.repos import recommendation_publications
    for module, owner, methods in (
        (radio_candidates, RadioCandidates, ('_stored_intensity_candidates', 'retain', '_energy')),
        (stored_radio, PreparedRadio, ('tracks', 'intensity_pool')),
    ):
        path = Path(module.__file__).relative_to(Path.cwd()).as_posix()
        source = subprocess.run(['git', 'show', revision + ':' + path], check=True, capture_output=True, text=True).stdout
        report.setdefault('baseline_sources', {})[path] = hashlib.sha256(source.encode()).hexdigest()
        node = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == owner.__name__)
        namespace = dict(vars(module))
        for method in methods:
            definition = next(child for child in node.body if isinstance(child, ast.FunctionDef) and child.name == method)
            exec(compile(ast.Module(body=[definition], type_ignores=[]), f'{revision}:{path}', 'exec'), namespace)
            original = namespace[method]
            if method == '_stored_intensity_candidates' and 'heads' not in (original.__kwdefaults__ or {}):
                def legacy_intensity(self, *args, heads=False, original=original):
                    return original(self, *args)
                original = legacy_intensity
            monkeypatch.setattr(owner, method, original)

    # The baseline hydration method now belongs to the controller. Its pure
    # projection still accepts the same captured pages and catalog facts.
    path = 'src/yt_playlist/repos/recommendation_publications.py'
    source = subprocess.run(['git', 'show', revision + ':' + path], check=True, capture_output=True, text=True).stdout
    report.setdefault('baseline_sources', {})[path] = hashlib.sha256(source.encode()).hexdigest()
    owner = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
                 and node.name == 'RecommendationPublicationsRepo')
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == '_hydrate_pages')
    namespace = dict(vars(recommendation_publications))
    exec(compile(ast.Module(body=[method], type_ignores=[]), f'{revision}:{path}', 'exec'), namespace)
    monkeypatch.setattr(RecommendationPublications, '_hydrate_pages', namespace['_hydrate_pages'])


def _measure(store, report, monkeypatch, report_path, captured_now, values, *, profiled, warm):
    controller = None
    try:
        saved, _ = store.radio_checkpoints.load()
        assert saved and not saved['stopped'], 'Profile requires a saved ongoing Radio session'
        tracks = saved['tracks']
        video = tracks[min(saved['position'], len(tracks) - 1)]['video_id']

        class Bridge:
            connected = True
            now_playing = {'video_id': video, 'playlist_ytm_id': saved['playlist'], 'paused': False}

            @property
            def now_playing_seen_at(self):
                return time.monotonic()

        controller = PlaylistRadio(SimpleNamespace(store=store, bridge=Bridge(), now_fn=lambda: captured_now,
            client_provider=lambda: {saved['identity']: object()}))
        monkeypatch.setattr(controller.work, 'start_thread', lambda *args, **kwargs: None)
        assert controller.join()['active']
        controller.ctx.radio = controller
        if warm:
            controller.session.candidates.planning_model().rng.seed(1105)
        report['model_ready_at_apply'] = controller.session.candidates.model_ready
        before = {'tracks': list(controller.session.tracks), 'heard': list(controller.session.heard)}

        def timed(name, operation):
            @wraps(operation)
            def call(*args, **kwargs):
                wall, cpu = time.perf_counter(), time.thread_time()
                try:
                    return operation(*args, **kwargs)
                finally:
                    values = report['timings'].setdefault(name, dict(calls=0, wall_s=0., cpu_s=0.))
                    values['calls'] += 1
                    values['wall_s'] += time.perf_counter() - wall
                    values['cpu_s'] += time.thread_time() - cpu
            return call

        for owner, name in ((RadioCandidates, '__init__'), (RadioCandidates, '_stored_intensity_candidates'),
                            (RadioCandidates, '_score'), (RadioCandidates, '_energy'), (RadioCandidates, '_genre_modes'),
                            (RadioCandidates, 'refresh_exclusions'), (PreparedRadio, 'intensity_pool'),
                            (RecommendationPublications, 'candidate_pages'),
                            (RecommendationPublications, '_hydrate_pages'), (musical_character, 'load')):
            monkeypatch.setattr(owner, name, timed(owner.__name__ + '.' + name, getattr(owner, name)))
        initialize = RadioCandidates.__init__

        def seeded(self, *args, **kwargs):
            initialize(self, *args, **kwargs)
            self.rng.seed(1105)

        monkeypatch.setattr(RadioCandidates, '__init__', seeded)
        app = FastAPI()
        app.include_router(radio_session.build(controller.ctx))
        profile = cProfile.Profile()
        with TestClient(app) as client:
            started = time.perf_counter()
            # The endpoint's work executes on its worker, so profile that same
            # controller entry rather than only profiling the HTTP client.
            tune = controller.tune

            def measure_tune(*args, **kwargs):
                wall, cpu = time.perf_counter(), time.thread_time()
                try:
                    with profile if profiled else nullcontext():
                        return tune(*args, **kwargs)
                finally:
                    report['worker_seconds'] = time.perf_counter() - wall
                    report['worker_cpu_seconds'] = time.thread_time() - cpu

            monkeypatch.setattr(controller, 'tune', measure_tune)
            response = client.post('/radio/tune', json=values)
            report['apply_seconds'] = time.perf_counter() - started
            report['http_status'] = response.status_code
            result = response.json()
            report['result'] = result
        report['saved_tuning'] = store.radio_checkpoints.load()[0]['tuning']
        report['mode_count'] = len(controller.session.candidates.pools)
        report['history_unchanged'] = controller.session.heard == before['heard']
        report['tracks_unchanged'] = controller.session.tracks == before['tracks']
        report['directions'] = [[track['video_id'] for track in branch['tracks']]
                                for branch in result.get('directions', [])]
        if report['profiled']:
            profile.dump_stats(str(report_path.with_suffix('.pstats')))
            stats = pstats.Stats(profile)
            report['profile_top'] = [{'file': key[0], 'line': key[1], 'function': key[2],
                                     'calls': value[1], 'own_seconds': value[2], 'cumulative_seconds': value[3]}
                                    for key, value in sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:35]]
        assert result['ok'] and result['tuning'] == report['saved_tuning'] == values
        assert report['history_unchanged'] and report['tracks_unchanged']
        assert len(report['directions']) == 4 and all(len(branch) == 1 for branch in report['directions'])
        assert len({branch[0] for branch in report['directions']}) == 4
        assert all(branch['filling'] for branch in result['directions'])
        report['apply_work'] = {name: dict(times) for name, times in report['timings'].items()}
        started = time.perf_counter()
        with controller.operation:
            controller.listening.fill_tuned_streams(controller.session, cancelled=lambda: False)
        report['fill_seconds'] = time.perf_counter() - started
        final = controller.status()
        assert final['menu_id'] == result['menu_id']
        for first, filled in zip(result['directions'], final['directions'], strict=True):
            assert not filled['filling'] and filled['id'] == first['id']
            assert filled['tracks'][0]['video_id'] == first['tracks'][0]['video_id']
            assert len({track['key'] for track in filled['tracks']}) == len(filled['tracks'])
    finally:
        report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
        if controller:
            controller.suspend()
            controller.work.stop_executor()
