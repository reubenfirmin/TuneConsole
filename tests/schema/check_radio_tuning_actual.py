"""Opt-in profiling of complete Apply on a disposable actual-library copy."""
import cProfile
import ast
from contextlib import nullcontext
from functools import wraps
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
from yt_playlist.core.store import Store
from yt_playlist.rec import musical_character
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.routes import radio_session


def test_actual_library_tuning(request, tmp_path, monkeypatch):
    source = request.config.getoption('--schema-source-db')
    output = request.config.getoption('--radio-tuning-report')
    if not source or not output:
        pytest.skip('Supply --schema-source-db and --radio-tuning-report for this opt-in profile')
    report_path = report_file(output).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'python': sys.version, 'source': source, 'source_access': 'read-only copy',
              'provider': 'forbidden; fresh playback presence controlled', 'sources': {}}
    for name in ('rec/radio_candidates.py', 'rec/stored_radio.py', 'rec/radio_directions.py',
                 'repos/recommendation_publications.py', 'core/db_reads.py', 'core/store.py',
                 'core/preparation/listening_views.py', 'web/radio_listening.py',
                 'web/radio_session.py', 'web/static/radio.js'):
        report['sources'][name] = hashlib.sha256((Path('src/yt_playlist') / name).read_bytes()).hexdigest()
    destination = tmp_path / 'radio-tuning.db'
    started = time.perf_counter()
    destination = copy_readonly_library(Path(source).resolve(strict=True), destination, monkeypatch, report)
    report['copy_seconds'] = time.perf_counter() - started
    store = Store.open_current(destination)
    try:
        saved, _ = store.radio_checkpoints.load()
        report['checkpoint_sha256'] = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        report['dataset'] = {name: store.conn.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                             for name in ('recordings', 'catalog_entries', 'play_events', 'listening_observations')}
        baseline = request.config.getoption('--radio-tuning-baseline')
        if baseline:
            report['baseline_revision'] = baseline
        captured_now = time.time()
        variants = ('baseline', 'optimized') if baseline else ('optimized',)
        for profiled in (False, True):
            for variant in variants:
                # Every measurement starts with the exact same checkpoint,
                # clock, library and random seed. Only private output changes.
                _, revision = store.radio_checkpoints.load()
                store.radio_checkpoints.save(json.dumps(saved), expected=revision)
                name = variant + ('_profile' if profiled else '')
                report[name] = {'timings': {}, 'profiled': profiled}
                report['phase'] = name
                report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
                with monkeypatch.context() as measured:
                    if variant == 'baseline':
                        _baseline(measured, baseline, report)
                    _measure(store, report[name], measured,
                             report_path.with_name(report_path.stem + '-' + name + '.json'),
                             captured_now, profiled=profiled)
                if baseline and variant == 'optimized':
                    counterpart = 'baseline' + ('_profile' if profiled else '')
                    assert report[counterpart]['directions'] == report[name]['directions'], 'Optimization changed seeded continuations'
        report['phase'] = 'complete'
    finally:
        report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
        store.conn.close()


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
        (recommendation_publications, RecommendationPublicationsRepo, ('_hydrate_pages',)),
    ):
        path = Path(module.__file__).relative_to(Path.cwd()).as_posix()
        source = subprocess.run(['git', 'show', revision + ':' + path], check=True, capture_output=True, text=True).stdout
        report.setdefault('baseline_sources', {})[path] = hashlib.sha256(source.encode()).hexdigest()
        node = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == owner.__name__)
        namespace = dict(vars(module))
        for method in methods:
            definition = next(child for child in node.body if isinstance(child, ast.FunctionDef) and child.name == method)
            exec(compile(ast.Module(body=[definition], type_ignores=[]), f'{revision}:{path}', 'exec'), namespace)
            monkeypatch.setattr(owner, method, namespace[method])


def _measure(store, report, monkeypatch, report_path, captured_now, *, profiled):
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
                            (RadioCandidates, '_score'), (RadioCandidates, '_energy'),
                            (RadioCandidates, 'refresh_exclusions'), (PreparedRadio, 'intensity_pool'),
                            (RecommendationPublicationsRepo, 'candidate_pages'),
                            (RecommendationPublicationsRepo, '_hydrate_pages'), (musical_character, 'load')):
            monkeypatch.setattr(owner, name, timed(owner.__name__ + '.' + name, getattr(owner, name)))
        initialize = RadioCandidates.__init__

        def seeded(self, *args, **kwargs):
            initialize(self, *args, **kwargs)
            self.rng.seed(1105)

        monkeypatch.setattr(RadioCandidates, '__init__', seeded)
        app = FastAPI()
        app.include_router(radio_session.build(controller.ctx))
        values = {'comfort': .17, 'theme': .5, 'energy': .31}
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
        assert len(report['directions']) == 4 and all(len(branch) == 6 for branch in report['directions'])
        assert all(len(branch) == len(set(branch)) for branch in report['directions'])
    finally:
        report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
        if controller:
            controller.suspend()
            controller.work.stop_executor()
