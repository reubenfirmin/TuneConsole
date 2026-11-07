"""Startup and full Home HTTP probes on manager-owned disposable libraries.

The empty case establishes setup availability, not time to first music. The
installed case checks already useful content without admitting a model build.
The cold case rebuilds musical profiles and vibe scores from retained inputs.
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

from yt_playlist.core import vibe_model_files
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.store import Store
from yt_playlist.rec import home_vibe, vibe_joint_model as model, vibe_joint_preparation as preparation
from yt_playlist.rec.progressive import Preparation
from yt_playlist.web.app import create_app


@contextmanager
def serving_only():
    """Cover HTTP handler threads as well as the caller during startup."""
    original_fit, original_read = model.fit, vibe_model_files.VibeModelFiles.read
    calls = {'fits': 0, 'model_reads': 0}

    def fit(*args, **kwargs):
        calls['fits'] += 1
        raise AssertionError('Startup or Home request attempted fitting')

    def read(*args, **kwargs):
        calls['model_reads'] += 1
        raise AssertionError('Startup or Home request attempted loading a model')

    model.fit, vibe_model_files.VibeModelFiles.read = fit, read
    try:
        yield calls
    finally:
        model.fit, vibe_model_files.VibeModelFiles.read = original_fit, original_read


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


def run_cold(output, *, candidate, seconds=300):
    """Retain real catalog/provider/sample inputs, removing this feature's caches.

    This is a cold musical-profile/vibe rebuild on an installed catalog. Other
    recommendation domains and earned navigation access remain installed state;
    a first new score is not a claim about first-account progression or menus.
    """
    database = Path(os.environ.get('TUNECONSOLE_TEST_LIBRARY', ''))
    if (os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1' or database != Path('/library/state.db')
            or output.parent.resolve() != Path('/reports')):
        raise ValueError('Use scripts/test-library.py with a private writable snapshot and /reports output')
    previous = preparation.QUALIFICATIONS
    report = {'candidate': candidate, 'configuration': model.configuration(),
              'operational_acceptance': False, 'seconds_limit': seconds,
              'limits': ['Cold musical-profile/vibe caches on a real installed catalog, not a new account.',
                         'Other recommendation caches and earned navigation access are retained.',
                         'First usable score means a newly prepared score for a playable saved recording.',
                         'HTTP requests and background preparation run sequentially in this startup check.',
                         'Python imports and cache removal precede the startup timer.']}
    preparation.QUALIFICATIONS = ({preparation.config_identity(): 'isolated-cold-startup-rehearsal'}
                                 if candidate else {})
    store = None
    worker = None
    original_fit = model.fit
    try:
        store = Store(database)
        store.init_schema()
        raw_tables = ('recordings', 'catalog_entries', 'library_memberships', 'sample_attributes',
                      'rec_musical_sources', 'rec_musical_links')
        report['retained_inputs'] = {table: store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                     for table in raw_tables}
        tables = ('recording_vibes', 'prediction_model_generations', 'predictions',
                  'measurement_samples', 'prediction_measurement_roots', 'measurements',
                  'rec_musical_profiles', 'rec_musical_source_profiles')
        report['removed_cache_rows'] = {table: store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                        for table in tables}
        with store.conn.atomic():
            for table in tables:
                store.conn.execute('DELETE FROM ' + table)
            for vibe in preparation.VIBES:
                store.conn.execute('DELETE FROM settings WHERE key IN (?,?)',
                    (vibe_model_files.STATE_PREFIX + vibe, vibe_model_files.HEAD_PREFIX + vibe))
            store.conn.execute('DELETE FROM rec_content_pending')
            store.conn.execute('INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings')
            store.conn.execute('INSERT OR IGNORE INTO rec_musical_pending(source_key,revision) '
                               'SELECT source_key,revision FROM rec_musical_sources')
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        store.conn.close()
        store = None
        started = time.monotonic()
        report['initial_home'] = probe(database, output.parent, 'cold-initial')
        assert report['initial_home']['ranked_results'] == report['initial_home']['cached_scores'] == 0
        store = Store(database)
        store.init_schema()
        worker = Preparation(store)
        calls = []

        def fit(rows, vibe, *args, **kwargs):
            observation = {'vibe': vibe, 'originals': len(rows), 'at_seconds': time.monotonic() - started}
            calls.append(observation)
            at = time.monotonic()
            try:
                return original_fit(rows, vibe, *args, **kwargs)
            finally:
                observation['seconds'] = time.monotonic() - at

        model.fit = fit
        report['fits_before_first_score'] = calls
        with store.priority.background(check_stopping):
            for turn in range(100000):
                if time.monotonic() - started >= seconds:
                    report['outcome'] = 'bounded-incomplete'
                    break
                worker.capture(time.time())
                first = store.conn.execute('SELECT r.identity_key,v.vibe,v.source FROM recording_vibes v '
                    'JOIN recordings r ON r.id=v.recording_id WHERE EXISTS ('
                    'SELECT 1 FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id '
                    'JOIN youtube_items y ON y.video_id=e.video_id '
                    'WHERE e.recording_id=r.id AND COALESCE(y.available,1)<>0) LIMIT 1').fetchone()
                if first:
                    report.update(outcome='first-score-ready', first_new_score_seconds=time.monotonic() - started,
                                  first_score=dict(first), turns=turn + 1)
                    break
            else:
                report['outcome'] = 'bounded-incomplete'
        worker.close()
        worker = None
        model.fit = original_fit
        report['queue_after'] = {table: store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                 for table in ('rec_content_pending', 'rec_musical_pending', 'rec_vibe_pending')}
        report['retained_inputs_unchanged'] = all(store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                                 == count for table, count in report['retained_inputs'].items())
        report['foreign_key_failures'] = [tuple(row) for row in store.conn.execute('PRAGMA foreign_key_check')]
        store.conn.close()
        store = None
        report['reopened_home'] = probe(database, output.parent, 'cold-reopened')
        report['seconds'] = time.monotonic() - started
    finally:
        model.fit = original_fit
        preparation.QUALIFICATIONS = previous
        if worker is not None:
            worker.close()
        if store is not None:
            store.conn.close()
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report
