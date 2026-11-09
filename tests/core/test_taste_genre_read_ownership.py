"""Taste reporting reads committed edits privately, without blocking writers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

from fastapi.testclient import TestClient
import numpy as np
import pytest

from tests.conftest import FakeClient
from tests.home_fixture_support import (
    home_control_routes, home_page_bytecode, home_routes_only, without_metadata_providers,
)
from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.store_fixture_support import reuse_reporting_reader, without_query_timing
from yt_playlist.core import catalog, genre_identity
from yt_playlist.rec import home_cards, home_prepared, mix_tuning, onboarding, rec_params, surfaces, taste_analysis, taste_viz
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('without_query_timing')


def _library(store):
    account = store.accounts.upsert_account('Main', 'credentials', None, True)
    with store.conn:
        for family in ('pop', 'jazz', 'house'):
            store.conn.execute('INSERT INTO genre_family VALUES (?,?) ON CONFLICT DO NOTHING',
                               (family, family))
        store.conn.execute("INSERT INTO genre_catalog(key,name,family_key,source) "
                           "VALUES ('house','House','house','curated') ON CONFLICT DO NOTHING")
        for key, name in (('owned-seed', 'Owned Seed'), ('owned-candidate', 'Owned Candidate')):
            store.conn.execute('INSERT INTO genre_catalog(key,name,family_key,source) VALUES (?,?,?,?)',
                               (key, name, 'pop', 'custom'))
    seed = store.tracks.upsert_track('seed', 'Anchor track', 'Band', None, 180)
    candidate = store.tracks.upsert_track('candidate', 'Candidate track', 'Band', None, 180)
    store.tracks.set_track_genre(seed, 'Owned Seed')
    store.tracks.set_track_genre(candidate, 'Owned Candidate')
    playlist = store.playlists.upsert_playlist(account, 'target', 'Target', 1, '', 100)
    store.playlists.set_playlist_tracks(playlist, [seed])
    other = store.playlists.upsert_playlist(account, 'other', 'Other', 2, '', 100)
    store.playlists.set_playlist_tracks(other, [seed, candidate])
    store.history.record_history_plays(account, 100, ['anchor track|band'], source_timezone='UTC')
    store.rec_model.replace_rec_vectors([
        ('anchor track|band', np.array([1, 0], dtype=np.float32).tobytes()),
        ('candidate track|band', np.array([0, 1], dtype=np.float32).tobytes())])
    store.settings.set_setting('rec_dim', '2')
    store.discovery.upsert_discovered_track('cold track|band', 'cold', 'Cold track', 'Band', '',
        None, 'Owned Seed', '2000', None, 100, duration_s=180)
    return account, playlist


def _calculations(store, playlist):
    return {
        'facets': lambda: taste_analysis.playlist_facets(store, playlist)['genres'],
        'breadth': lambda: taste_analysis.taste_breadth(store)['families'],
        'fingerprint': lambda: taste_analysis.taste_fingerprint(store, 100)['families'],
        'overview': lambda: taste_viz.overview(store, 100)['genres'],
        'transparency': lambda: taste_viz.model_transparency(store, 100)['genres'],
        'influence': lambda: taste_viz.playlist_influence_panel(store)['contexts'],
        'centroids': lambda: taste_viz.centroid_tilt_panel(store, 100)['families'],
    }


def _families(name, result):
    if name == 'breadth':
        return set(result)
    if name == 'influence':
        return {genre for row in result for genre in row['genres']}
    return {row['name'] for row in result}


@pytest.fixture
def live_tuning_input(installed, monkeypatch, home_page_bytecode):
    from tests.test_home import _publish_existing_listener_mix
    from tests.recommendation_support import publish_bundles
    from yt_playlist.web import app as webapp

    store, _ = installed
    # Fixture preparation has no competing writer. Restore fresh reader
    # admission before the actual concurrent genre calculation starts.
    with reuse_reporting_reader(store, monkeypatch):
        account, _ = _library(store)
        _publish_existing_listener_mix(store)
        pool = [{'key': key, **{field: track[field] for field in
                 ('title', 'artist', 'album', 'video_id', 'thumbnail', 'genre')}} for key, track in
                store.rec_query.group_candidates(store.rec_query.library_keys()).items()]
        publish_bundles(store, {'all': {'wheelhouse': pool, 'explore': pool, 'comfort': pool},
                               '_meta': {'full_pools': True, 'unmodeled': True}}, 1000)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)
    return account


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('commit', [False, True], ids=['rollback', 'commit'])
def test_live_home_tuning_calculates_privately_then_publishes(
        installed, live_tuning_input, monkeypatch, caplog, commit,
        home_control_routes, without_metadata_providers):
    store, _ = installed
    account = live_tuning_input
    assert not store.rec_proposals.get_proposals('home_tuned')
    client = TestClient(create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 1000,
                                  background_work=False),
                        base_url='http://127.0.0.1')
    start, entered, release = (threading.Event() for _ in range(3))
    observed, published = [], []
    cards, remember = home_cards.HomeCards.cards, home_prepared.remember
    calculating = False

    def writer():
        assert start.wait(10)
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='owned-seed'")
                if commit:
                    store.conn.commit()
                entered.set()
                assert release.wait(10)
            finally:
                store.conn.rollback()

    def calculate(service, request):
        nonlocal calculating
        assert service.store._read_only
        assert not store._lock.held_by_current_thread
        assert service.store.genres.feature_axes(['Owned Seed'])['Owned Seed'][0] == 'pop'
        start.set()
        assert entered.wait(10)
        calculating = True
        try:
            observed.append(service.store.genres.feature_axes(['Owned Seed'])['Owned Seed'][0])
            return cards(service, request)
        finally:
            calculating = False
            release.set()
            work.result(5)

    def publish(owner, payload, result, now, *, tuning=None):
        assert owner is store and not calculating
        assert tuning is not None and result.protos
        value = remember(owner, payload, result, now, tuning=tuning)
        published.append((tuning, [p['experiment_id'] for p in result.protos]))
        return value

    monkeypatch.setattr(home_cards.HomeCards, 'cards', calculate)
    monkeypatch.setattr(home_prepared, 'remember', publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(writer)
        try:
            response = client.post('/home/breadth', data={'breadth_bias': '.5'})
        finally:
            start.set()
            release.set()
        work.result(5)
    assert response.status_code == 200
    assert 'home cards render failed' not in caplog.text
    assert observed == ['jazz' if commit else 'pop'] and len(published) == 1
    tuned = store.rec_proposals.get_proposals('home_tuned')
    assert tuned['built_at'] == 1000 and tuned['tuning'] == published[0][0]
    assert [p['experiment_id'] for p in tuned['menus'][0]['protos']] == published[0][1]
    assert rec_params.get_param(store, 'breadth_bias') == .5
    assert store.genres.feature_axes(['Owned Seed'])['Owned Seed'][0] == ('jazz' if commit else 'pop')


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('commit', [False, True], ids=['rollback', 'commit'])
def test_taste_workflows_keep_one_view_without_blocking_writes(installed, monkeypatch, commit):
    store, _ = installed
    _, playlist = _library(store)
    for name, calculate in _calculations(store, playlist).items():
        with store.conn:
            store.conn.execute("UPDATE genre_catalog SET family_key='pop' WHERE key='owned-seed'")
        start, entered, release = (threading.Event() for _ in range(3))
        observed = []
        family = genre_identity.family
        axis_matches = genre_identity.axis_match_rows

        def writer():
            assert start.wait(5), name
            with store._lock:
                store.conn.execute('BEGIN IMMEDIATE')
                try:
                    store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='owned-seed'")
                    if commit:
                        store.conn.commit()
                    entered.set()
                    assert release.wait(5), name
                finally:
                    store.conn.rollback()

        @contextmanager
        def concurrent_edit(conn):
            assert conn is not store.conn, name
            assert not store._lock.held_by_current_thread, name
            start.set()
            assert entered.wait(5), name
            try:
                yield
            finally:
                release.set()
                work.result(5)

        def resolve_matches(conn, labels):
            labels = list(labels)
            if any(label == 'Owned Seed' for label, _ in labels) and not observed:
                with concurrent_edit(conn):
                    result = axis_matches(conn, labels)
                    axes = genre_identity.axes_from_matches(result)
                    observed.append((axes['Owned Seed'][0], conn is store.conn))
                return result
            return axis_matches(conn, labels)

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
            patch.setattr(genre_identity, 'axis_match_rows', resolve_matches)
            work = pool.submit(writer)
            try:
                result = calculate()
            finally:
                start.set()
                release.set()
            work.result(5)
        assert result, name
        assert observed == [('jazz' if commit else 'pop', False)], name
        expected = ({'jazz'} if name == 'facets' else {'pop', 'jazz'}) if commit else {'pop'}
        assert _families(name, result) == expected, (name, result)
        assert family(store.conn, 'Owned Seed') == ('jazz' if commit else 'pop')
        if name == 'fingerprint':
            seed_family = next(row for row in result if row['name'] == ('jazz' if commit else 'pop'))
            assert 'owned-seed' in {row['name'] for row in seed_family['subgenres']}


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_taste_workflows_reuse_callers_uncommitted_view(installed):
    store, _ = installed
    _, playlist = _library(store)
    store.rec_model.set_weight('genre:jazz', 0, lo=0, now=100)
    with store._lock:
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='owned-seed'")
            for name, calculate in _calculations(store, playlist).items():
                assert 'jazz' in _families(name, calculate()), name
                assert store.conn.in_transaction, name
            assert not surfaces.cold_candidates(store, 100)
            assert not surfaces.complete_playlist(store, playlist, now=100)
            assert store.conn.in_transaction
        finally:
            store.conn.rollback()
    assert store.genres.feature_axes(['Owned Seed'])['Owned Seed'][0] == 'pop'


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('commit', [False, True], ids=['rollback', 'commit'])
def test_facets_pin_membership_before_loading_track_metadata(installed, monkeypatch, commit):
    store, _ = installed
    _, playlist = _library(store)
    seed = store.conn.execute("SELECT id FROM tracks WHERE video_id='seed'").fetchone()[0]
    start, entered, release = (threading.Event() for _ in range(3))

    def writer():
        assert start.wait(5)
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.playlists.repository._set_playlist_tracks(playlist, [])
                catalog.choose_field(store.conn, seed, 'genre', 'Jazz')
                if commit:
                    store.conn.commit()
                entered.set()
                assert release.wait(5)
            finally:
                store.conn.rollback()

    owner = type(store.playlists)
    members = owner.get_playlist_track_keys

    def after_members(repo, pid):
        result = members(repo, pid)
        if pid == playlist and not start.is_set():
            start.set()
            if not entered.wait(5):
                work.result(1)
                pytest.fail('Concurrent membership writer did not finish its edit')
        return result

    monkeypatch.setattr(owner, 'get_playlist_track_keys', after_members)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(writer)
        try:
            facets = taste_analysis.playlist_facets(store, playlist)
        finally:
            start.set()
            release.set()
        work.result(5)
    assert facets['genres'] == [{'name': 'jazz' if commit else 'pop', 'keys': ['anchor track|band']}]
    assert facets['tracks'] == [{'key': 'anchor track|band', 'title': 'Anchor track', 'artist': 'Band'}]
    assert store.rec_query.track_genres(['anchor track|band']) == {
        'anchor track|band': 'Jazz' if commit else 'Owned Seed'}
    assert store.playlists.get_playlist_track_keys(playlist) == (set() if commit else {'anchor track|band'})


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('surface', ['suggestions', 'onboarding'])
@pytest.mark.parametrize('commit', [False, True], ids=['rollback', 'commit'])
def test_http_candidates_survive_concurrent_genre_edits(installed, monkeypatch, surface, commit):
    store, _ = installed
    account, playlist = _library(store)
    store.rec_model.set_weight('genre:jazz', 0, lo=0, now=100)
    start, entered, release = (threading.Event() for _ in range(3))
    observed = []

    class LocalOnly(FakeClient):
        def get_home(self):
            return []

    def writer():
        assert start.wait(10)
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='owned-seed'")
                if commit:
                    store.conn.commit()
                entered.set()
                assert release.wait(10)
            finally:
                store.conn.rollback()

    owner = type(store.rec_query) if surface == 'suggestions' else type(store.rec_model)
    method = 'track_genres' if surface == 'suggestions' else 'muted_artists'
    read = getattr(owner, method)

    def after_metadata(repo, *args, **kwargs):
        result = read(repo, *args, **kwargs)
        if not start.is_set():
            start.set()
            assert entered.wait(5)
        return result

    axis_matches = genre_identity.axis_match_rows

    def resolve(conn, labels):
        labels = list(labels)
        result = axis_matches(conn, labels)
        if 'Owned Seed' in result and entered.is_set() and not observed:
            axes = genre_identity.axes_from_matches(result)
            observed.append((axes['Owned Seed'][0], conn is store.conn))
            release.set()
            work.result(5)
        return result

    app = create_app(store, lambda: {account: LocalOnly()}, now_fn=lambda: 100, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        monkeypatch.setattr(owner, method, after_metadata)
        monkeypatch.setattr(genre_identity, 'axis_match_rows', resolve)
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(writer)
            try:
                path = f'/playlist/{playlist}/suggestions' if surface == 'suggestions' else '/home/onboard/radio'
                response = client.get(path)
            finally:
                start.set()
                release.set()
            work.result(5)
    assert response.status_code == 200
    title = 'Candidate track' if surface == 'suggestions' else 'Cold track'
    # The now-committed Jazz mute governs these candidates. An uncommitted
    # genre edit stays invisible to the reporting connection.
    assert (title in response.text) is (not commit)
    assert observed == [('jazz' if commit else 'pop', False)]


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_expiry_current_steering_and_provider_fallback_remain_live(installed, monkeypatch):
    store, _ = installed
    _library(store)
    calculations = (
        lambda now: taste_analysis.taste_fingerprint(store, now),
        lambda now: taste_viz.model_transparency(store, now),
        lambda now: surfaces.cold_candidates(store, now),
        lambda now: onboarding.radio_sample(store, FakeClient(), now),
    )
    for calculate in calculations:
        with mix_tuning.adjusting(store, 100):
            store.rec_model.set_lean('genre:pop', 1.4, 100)
        calculate(100 + mix_tuning.TTL_S + 1)
        assert not store.rec_model.get_leans()
        assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None
    store.rec_model.set_weight('genre:pop', 1.7, now=100)
    with mix_tuning.adjusting(store, 100):
        store.rec_model.set_lean('genre:pop', 1.1, 100)
    pop = next(row for row in taste_analysis.taste_fingerprint(store, 100)['families']
               if row['name'] == 'pop')
    assert pop['effective'] == pytest.approx(1.87)

    active = 0
    read_view = store.read_view

    @contextmanager
    def tracked_view():
        nonlocal active
        with read_view() as reader:
            active += 1
            try:
                yield reader
            finally:
                active -= 1

    class HomeFallback(FakeClient):
        def get_home(self):
            assert active == 0
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(store.settings.set_setting, 'fallback-observed', '1').result(5)
            return [{'contents': [{'videoId': 'fallback', 'title': 'Provider track',
                                    'artists': [{'name': 'Provider'}], 'duration_seconds': 180}]}]

    monkeypatch.setattr(store, 'read_view', tracked_view)
    sample = onboarding.radio_sample(store, HomeFallback(), 100, n=2)
    assert [row['video_id'] for row in sample] == ['cold', 'fallback']
    assert store.settings.get_setting('fallback-observed') == '1'
