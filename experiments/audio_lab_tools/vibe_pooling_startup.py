"""Startup and full Home HTTP probes on manager-owned disposable libraries.

The empty case establishes setup availability, not time to first music. The
installed case checks already useful content without admitting a model build.
Run each qualification setting in a separate copy of the same baseline.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from fastapi.testclient import TestClient

from yt_playlist.core.store import Store
from yt_playlist.rec import home_vibe, vibe_joint_model as model, vibe_joint_preparation as preparation
from yt_playlist.repos import vibe_models
from yt_playlist.web.app import create_app


@contextmanager
def serving_only():
    """Cover HTTP handler threads as well as the caller during startup."""
    original_fit, original_read = model.fit, vibe_models.read
    calls = {'fits': 0, 'model_reads': 0}

    def fit(*args, **kwargs):
        calls['fits'] += 1
        raise AssertionError('Startup or Home request attempted fitting')

    def read(*args, **kwargs):
        calls['model_reads'] += 1
        raise AssertionError('Startup or Home request attempted loading a model')

    model.fit, vibe_models.read = fit, read
    try:
        yield calls
    finally:
        model.fit, vibe_models.read = original_fit, original_read


def probe(database, output, name):
    """Use all ordinary routers, templates and middleware, without background jobs."""
    with serving_only() as calls:
        report = _probe(database, output, name)
    assert not any(calls.values()), calls
    return {**report, 'guards': calls}


def _probe(database, output, name):
    started = time.monotonic()
    store = Store(database)
    try:
        store.init_schema()
        initialized = time.monotonic() - started
        keys = [row[0] for row in store.conn.execute('SELECT DISTINCT r.identity_key '
            'FROM recordings r JOIN recording_vibes v ON v.recording_id=r.id ORDER BY r.id LIMIT 128')]
        before = store.vibe_predictions.fit_scores(keys, 'relax') if keys else {}
        heads_before = {vibe: preparation.state(store, vibe) for vibe in preparation.VIBES}
        app = create_app(store, lambda: {}, background_work=False)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            response = client.get('/')
            first_home = time.monotonic() - started
            assert response.status_code == 200, response.text[:1000]
            document = response.text
            (output / (name + '-home.html')).write_text(document)
            with store.priority.request(), store.read_view() as reader:
                ranked = sum(len(reader.vibe_predictions.ranked(vibe, 100)) for vibe in home_vibe.PRESETS)
                scores = reader.vibe_predictions.fit_scores(keys, 'relax') if keys else {}
            cached_useful = time.monotonic() - started if ranked and scores else None
            assert scores == before
            responses = []
            for route in ('/home/cards', '/home/vibe/status', '/'):
                at = time.monotonic()
                fragment = client.get(route)
                responses.append({'path': route, 'status': fragment.status_code,
                                  'seconds': time.monotonic() - at, 'bytes': len(fragment.content)})
                # An empty installation can legitimately lock these fragment
                # destinations. Record the actual gate; do not force access.
                assert fragment.status_code in (200, 409), fragment.text[:1000]
                if route == '/':
                    assert fragment.status_code == 200
                part = route.strip('/').replace('/', '-') or 'root'
                (output / (name + '-' + part + '.html')).write_text(fragment.text)
        assert heads_before == {vibe: preparation.state(store, vibe) for vibe in preparation.VIBES}
        assert before == (store.vibe_predictions.fit_scores(keys, 'relax') if keys else {})
        # Serving markers are evidence of document content, not visual acceptance.
        return {'initialized_seconds': initialized, 'first_home_seconds': first_home,
                'first_cached_music_seconds': cached_useful, 'ranked_results': ranked,
                'cached_scores': len(scores), 'home_bytes': len(document.encode()),
                'home_sha256': hashlib.sha256(document.encode()).hexdigest(),
                'responses': responses, 'model_states_unchanged': True,
                'model_states_present': sum(bool(value) for value in heads_before.values())}
    finally:
        store.conn.close()


def run(output, *, candidate):
    database = Path(os.environ.get('TUNECONSOLE_TEST_LIBRARY', ''))
    if (os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1' or database != Path('/library/state.db')
            or output.parent.resolve() != Path('/reports')):
        raise ValueError('Use scripts/test-library.py with a private writable snapshot and /reports output')
    previous = preparation.QUALIFICATIONS
    report = {'candidate': candidate, 'configuration': model.configuration(),
              'operational_acceptance': False,
              'timing_boundary': 'Store construction, schema initialization, app construction and first request; '
                                 'Python imports and qualification configuration precede the timer',
              'limits': ['Empty startup has no musical evidence and establishes setup availability only.',
                         'Installed startup retains the actual baseline caches; it is not a cold library rebuild.',
                         'All Home routers and HTTP middleware run, with unrelated background jobs disabled.',
                         'Cached score availability is separate from rendered menu readiness and visual quality.',
                         'This probe does not establish foreground latency during a completed refresh.']}
    preparation.QUALIFICATIONS = ({preparation.config_identity(): 'isolated-startup-rehearsal'}
                                 if candidate else {})
    try:
        with TemporaryDirectory(prefix='vibe-pooling-empty-startup-') as directory:
            report['empty'] = probe(Path(directory) / 'empty.db', output.parent, 'empty')
        report['empty_workspace_removed'] = not Path(directory).exists()
        report['installed'] = probe(database, output.parent, 'installed')
        report['reopened'] = probe(database, output.parent, 'reopened')
    finally:
        preparation.QUALIFICATIONS = previous
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report
