"""Opt-in #471 actual-library audit; native source is opened only for backup.

uv run pytest --test-lock-wait=60 -s tests/bench_progressive_recovery.py
The cold scenario deletes only derived state on its disposable copy and applies
controlled continuing metadata updates. All normal row/time/memory caps remain.
"""
from contextlib import contextmanager, ExitStack
from functools import wraps
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from tests.bench_svd_background import _backup_library, _saved_library_source
from yt_playlist.core.db_diagnostics import PublicationTimings
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import content_model, group_assignment, musical_preparation, progressive, taste_modes, vibe_preparation
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.repos.taste_groups import TasteGroupsRepo


class _CaptureIncomplete(AssertionError):
    """Keep the capture failure while still measuring a fit of accepted inputs."""


class _Costs:
    def __init__(self):
        self.stages = {}

    def record(self, name, wall, cpu):
        stage = self.stages.setdefault(name, {'calls': 0, 'wall_s': 0., 'cpu_s': 0., 'max_wall_s': 0.})
        stage['calls'] += 1
        stage['wall_s'] += wall
        stage['cpu_s'] += cpu
        stage['max_wall_s'] = max(stage['max_wall_s'], wall)

    @contextmanager
    def measure(self, name):
        started, cpu = time.monotonic(), time.thread_time()
        try:
            yield
        finally:
            self.record(name, time.monotonic() - started, time.thread_time() - cpu)

    def wrap(self, original, name):
        @wraps(original)
        def measured(*args, **kwargs):
            with self.measure(name):
                return original(*args, **kwargs)
        return measured


@contextmanager
def _capture_costs(costs):
    # Only this disposable audit process is instrumented. Nested totals overlap;
    # they identify expensive boundaries and must not be added together.
    targets = (
        (Store, '_make_reader', 'reader.open'),
        (Revisions, 'capture', 'snapshot.revisions'),
        (progressive.Preparation, 'repair', 'capture.repair'),
        (PreparedContentRepo, 'capture', 'capture.content_sql'),
        (MusicalProfilesRepo, 'recording_inputs', 'capture.musical_inputs_sql'),
        (musical_preparation, 'source_step', 'capture.source_step'),
        (musical_preparation, 'recordings', 'capture.musical_profiles'),
        (vibe_preparation, 'step', 'capture.vibe_fanout'),
        (vibe_preparation, 'recordings', 'capture.vibe_profiles'),
        (group_assignment, 'prepare', 'capture.group_assignments'),
        (TasteGroupsRepo, 'assignment_head', 'capture.group_regions'),
        (TasteGroupsRepo, 'inputs_for', 'capture.group_anchors'),
        (progressive, '_delta', 'capture.vector_delta'),
    )
    with ExitStack() as stack:
        for owner, name, label in targets:
            stack.enter_context(patch.object(owner, name, costs.wrap(getattr(owner, name), label)))
        yield


def _capture(preparation, scenario, now, report, costs):
    store = preparation.store
    started, cpu = time.monotonic(), time.process_time()
    capture = report['capture'] = {'limit_s': 180, 'complete': False, 'first_useful': None,
                                  'progress': [], 'costs': costs.stages,
                                  'cost_note': 'Nested stage totals overlap; do not add them.'}
    step = 0
    try:
        for step in range(2048):
            with costs.measure('audit.completion_check'):
                pending = store.prepared_content.has_pending()
                # Do not add a full-catalog anti-join and another reader before
                # every production slice. Verify coverage when the queue empties.
                if not pending:
                    with store.read_view() as reader:
                        missing = reader.prepared_content.missing_unqueued(1)
                        missing_previous = reader.prepared_content.missing_previous_inputs()
                    if not missing and not missing_previous:
                        capture['complete'] = True
                        break
            if time.monotonic() - started >= capture['limit_s']:
                raise _CaptureIncomplete('Content input capture exceeded 180 seconds')
            with costs.measure('capture.total'):
                preparation.capture(now + step * 5)
            # Time the first useful partial basis. Later captures update vectors
            # against it; intermediate basis refits are omitted to isolate cost.
            if scenario == 'cold-and-growing' and capture['first_useful'] is None:
                with costs.measure('audit.initial_fit'):
                    fitted = preparation.fit(now + step * 5)
                    if fitted:
                        status = preparation.status()
                        if status['library_vectors'] or status['discovered_vectors']:
                            capture['first_useful'] = {'wall_s': time.monotonic() - started,
                                                       'step': step, **status}
            if step % 32 == 0:
                with costs.measure('audit.progress'):
                    capture['progress'].append({'step': step, 'wall_s': time.monotonic() - started,
                                                **store.prepared_content.progress()})
        else:
            raise _CaptureIncomplete('Content input capture exceeded 2048 admissions')
    finally:
        # Keep first-useful timing and queue diagnostics even on an exception.
        # This also distinguishes production work from audit-only overhead.
        capture.update(steps=step, wall_s=time.monotonic() - started, cpu_s=time.process_time() - cpu)
        with costs.measure('audit.final_state'):
            with store.read_view() as reader:
                capture['missing_sample'] = reader.prepared_content.missing_unqueued(1)
                capture['missing_previous'] = reader.prepared_content.missing_previous_inputs()
            capture.update(musical_sources_due=musical_preparation.due(store),
                           vibe_due=vibe_preparation.due(store), **preparation.status())
    return step


@pytest.mark.parametrize('scenario', ['saved', 'cold-and-growing'])
def test_actual_library_basis_recovery(tmp_path, scenario):
    path = tmp_path / 'library.db'
    backup = _backup_library(_saved_library_source(), path)
    try:
        child = subprocess.run([sys.executable, '-m', 'tests.bench_progressive_recovery', str(path), scenario],
                               capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        (tmp_path / 'stdout.log').write_bytes(exc.stdout or b'')
        (tmp_path / 'stderr.log').write_bytes(exc.stderr or b'')
        pytest.fail(f'Recovery audit exceeded 300 seconds; diagnostics in {tmp_path}')
    (tmp_path / 'stdout.log').write_text(child.stdout)
    (tmp_path / 'stderr.log').write_text(child.stderr)
    print(json.dumps({'backup': backup, 'report': str(tmp_path / 'recovery.json')}), flush=True)
    assert child.returncode == 0, f'{child.stdout}\n{child.stderr}'
    print((tmp_path / 'recovery.json').read_text(), flush=True)


def _rss():
    # Linux current RSS, distinct from the process lifetime high-water mark.
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)


def _verify_vectors(store):
    with store.read_view() as reader:
        corpus = reader.prepared_content.corpus(progressive.Budget().max_rows)
    content, audio = {}, {}
    for key, payload, _digest in corpus:
        features = payload['library']
        if features is not None:
            if features['genre'] or features['year']:
                content[key] = features['genre'], features['year']
            if features['audio']:
                audio[key] = features['audio']
    model = content_model.build_content_model(content, audio)
    expected = {'library': [], 'discovered': []}
    for key, payload, _digest in corpus:
        for pool in expected:
            features = payload[pool]
            if features is not None:
                vector = content_model.encode_content(model, features['genre'], features['year'], features['audio'])
                if vector is not None:
                    expected[pool].append((key, vector.tobytes()))
    generation = store.rec_model.content_generation()
    assert json.loads(generation['model']) == model
    for pool in expected:
        assert generation[pool] == expected[pool], pool
    modes = taste_modes.discover_modes(store, snapshot=generation,
                                       genres={key: value[0] for key, value in content.items()})
    actual_modes = store.modes.list_modes(active_only=True)
    fields = ('label', 'size', 'families', 'rep_keys', 'space')
    def mode_row(mode):
        return json.dumps([mode[field] for field in fields]), mode['centroid'].tobytes().hex()
    assert sorted(map(mode_row, actual_modes)) == sorted(map(mode_row, modes))
    return {pool: len(rows) for pool, rows in expected.items()}


def _measure(path, scenario):
    limit_native_threads()
    store = Store(path)
    store.init_schema()
    report = {'scenario': scenario, 'python': sys.version, 'caps': vars(progressive.Budget()),
              'controlled_changes': [], 'modules': {}}
    for module in (content_model, progressive, taste_modes, musical_preparation, vibe_preparation,
                   group_assignment, sys.modules[PreparedContentRepo.__module__],
                   sys.modules[TasteGroupsRepo.__module__],
                   sys.modules[MusicalProfilesRepo.__module__], sys.modules[Revisions.__module__],
                   sys.modules[Store.__module__]):
        source = Path(module.__file__)
        report['modules'][module.__name__] = {'path': str(source),
                                             'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    stop = threading.Event()
    latencies, memory, publications, errors = [], [], [], []
    original_report = PublicationTimings.report
    observer = None
    costs, capture_failure = _Costs(), None
    phase = 'capture'

    def timed_publication(timing, outcome):
        if phase == 'capture':
            for name, values in timing.stages.items():
                costs.record('publication.' + name, values['wall_s'], values['cpu_s'])
            costs.record('publication.outcome.' + outcome, 0., 0.)
        else:
            publications.append({'outcome': outcome, 'stages': timing.stages})
        original_report(timing, outcome)

    PublicationTimings.report = timed_publication
    try:
        if scenario == 'cold-and-growing':
            with store.conn:
                for table in ('rec_content_vectors', 'rec_discovered_content_vectors',
                              'rec_prepared_content', 'rec_content_pending'):
                    store.conn.execute(f'DELETE FROM {table}')
                store.conn.executemany('DELETE FROM settings WHERE key=?', [(key,) for key in (
                    'rec_content_model', 'rec_content_generation', 'rec_content_genre_revision',
                    'rec_content_inputs', progressive.BASIS_STATE, progressive.FIT_STATE)])
            report['controlled_changes'] = ['Removed derived content inputs/vectors/model on copy',
                                             'Lost queue rebuilt by production maintenance audit',
                                             'Two recording BPM values change continually during fit']
        else:
            # Force one replacement-basis measurement on this copy. Input data
            # and existing serving vectors remain untouched.
            store.settings.delete_setting(progressive.BASIS_STATE)
            store.settings.delete_setting(progressive.FIT_STATE)
            report['controlled_changes'] = ['Cleared basis checkpoint/retry delay on copy']
        preparation = progressive.Preparation(store)
        report['before'] = preparation.status()
        now = time.time()
        try:
            with _capture_costs(costs):
                step = _capture(preparation, scenario, now, report, costs)
        except _CaptureIncomplete as exc:
            # A deadline still fails acceptance. First collect the fit/resource
            # evidence for the accepted cohort while unprocessed inputs remain.
            capture_failure = exc
            report['capture']['failure'] = str(exc)
            step = report['capture']['steps']
        phase = 'fit'
        if scenario == 'cold-and-growing':
            assert report['capture']['first_useful'] is not None, 'Cold preparation never produced useful vectors'
        hot = [row[0] for row in store.conn.execute('SELECT id FROM recordings ORDER BY id DESC LIMIT 2')]

        def observe():
            try:
                while not stop.is_set():
                    before = time.monotonic()
                    with store._lock, store.conn:
                        store.conn.execute("INSERT INTO settings(key,value) VALUES ('recovery-probe','1') "
                                           "ON CONFLICT(key) DO UPDATE SET value=value")
                        if scenario == 'cold-and-growing':
                            for i, recording in enumerate(hot):
                                store.conn.execute('UPDATE recordings SET bpm=? WHERE id=?',
                                                   (90 + len(latencies) % 100 + i, recording))
                    latencies.append(time.monotonic() - before)
                    memory.append(_rss())
                    stop.wait(.01)
            except BaseException as exc:
                errors.append(repr(exc))

        baseline = _rss()
        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        started, cpu = time.monotonic(), time.process_time()
        fitted = preparation.fit(now + step * 5 + 60)
        report['fit'] = {'completed': fitted, 'complete_input_coverage': report['capture']['complete'],
                          'wall_s': time.monotonic() - started,
                          'cpu_s': time.process_time() - cpu, **preparation.status()}
        stop.set()
        observer.join(5)
        assert not observer.is_alive() and not errors, errors
        peak = max([baseline, _rss(), *memory])
        ordered = sorted(latencies)
        report.update(rss_baseline_bytes=baseline, rss_sampled_peak_bytes=peak,
            rss_increase_bytes=peak - baseline,
            rss_limit_note='RSS increase is observed process growth, not an isolated allocation measurement.',
            foreground_samples=len(ordered), foreground_p95_s=ordered[int(.95 * (len(ordered) - 1))] if ordered else None,
            foreground_p99_s=ordered[int(.99 * (len(ordered) - 1))] if ordered else None,
            foreground_max_s=max(ordered, default=0), publications=publications)
        assert fitted, report['fit']
        assert latencies, 'No concurrent foreground observation'
        assert report['rss_increase_bytes'] <= preparation.budget.memory_bytes, report
        assert report['foreground_p95_s'] < .1 and report['foreground_p99_s'] < .25, report
        assert report['foreground_max_s'] < 1, report
        report['exact_verified_vectors'] = _verify_vectors(store)
        assert not progressive.maintenance_view(store)['blocked']
        if capture_failure is not None:
            raise capture_failure
    finally:
        stop.set()
        if observer is not None:
            observer.join(5)
        PublicationTimings.report = original_report
        report['after'] = progressive.Preparation(store).status()
        Path(path).with_name('recovery.json').write_text(json.dumps(report, indent=2) + '\n')
        store.conn.close()


if __name__ == '__main__':
    _measure(sys.argv[1], sys.argv[2])
