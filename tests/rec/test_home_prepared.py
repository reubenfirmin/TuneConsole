"""Worker-produced mixes are served intact; exposure survives disposable preparation."""
from concurrent.futures import ThreadPoolExecutor
from tests.derived_rating_support import seed_dislike, seed_like
from collections import Counter
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from tests.genre_catalog_support import bound_catalog
from tests.group_support import publish as publish_groups, recordings as grouped_recordings
from tests.home_fixture_support import home_routes_only, publish_menu, returning_listener, service
from tests.home_prepared_support import publication_inputs as controlled_publication_inputs, seed
from tests.recommendation_support import publish_bundles, seed_bundle_subjects, seed_publication_subjects
from tests.store_fixture_support import (_file_consumer_store, bounded_consumer_schema, initialize_consumer,
                                         mock_snapshot_coordinator, reuse_reporting_reader,
                                         without_query_timing)
from yt_playlist.core import schema_inspection
from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, home_cache, home_cards, home_prepared, home_themes, mix_tuning, mode_surfaces, rec_params, surfaces
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app


MENU_GENRES = (('jazz', 'Jazz'), ('techno', 'Techno'), ('classical', 'Classical'),
               ('ambient', 'Ambient'), ('trance', 'Trance'), ('indie rock', 'Indie Rock'),
               ('house', 'House'))


@pytest.fixture(scope='module')
def cached_menu_schema():
    with bounded_consumer_schema(MENU_GENRES) as schema:
        yield schema


@pytest.fixture
def menu_publication_store(tmp_path, cached_menu_schema, monkeypatch):
    """Private real publication/reopen SQL without unrelated taxonomy input."""
    from yt_playlist.core.db_diagnostics import DiagnosticConnection

    # These synchronous publication consumers keep real files, SQLite and
    # transactions. Per-query handle admission/diagnostics are lower owners.
    for operation in ('execute', 'executemany'):
        monkeypatch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))
    database = Store(tmp_path / 'published-home.db')
    with closing(database.conn):
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, cached_menu_schema)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with (reuse_reporting_reader(database, monkeypatch),
              schema_inspection.reuse_tables(database.conn)):
            yield database


@pytest.fixture(scope='module')
def uncontended_menu_schema():
    with bounded_consumer_schema(MENU_GENRES, snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def uncontended_menu_store(tmp_path, uncontended_menu_schema, monkeypatch):
    """Real menu SQL/files below successful snapshot coordination.

    These consumers own lossless publication, failed file replacement and
    feedback projection. Source-change races have separate guarded owners.
    The large-menu case still performs actual startup admission on disk reopen.
    """
    from yt_playlist.core.db_diagnostics import DiagnosticConnection

    # Keep the completed vocabulary bounded during real reopen too. Startup
    # otherwise imports the full unrelated taxonomy into this seven-genre input.
    bound_catalog(monkeypatch, entries=MENU_GENRES)
    for operation in ('execute', 'executemany'):
        monkeypatch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))
    database = Store(tmp_path / 'menu.db')
    with closing(database.conn):
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, uncontended_menu_schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with mock_snapshot_coordinator(database, monkeypatch):
            yield database


@pytest.fixture
def store(request, tmp_path, monkeypatch):
    # Consumer menus use only six genres, independently of ranking inputs.
    with ExitStack() as resources:
        if {'cached_menu', 'cached_menu_schema', 'publication_inputs', 'catalog_import', 'metadata_import'} & set(request.fixturenames):
            bound_catalog(monkeypatch, entries=MENU_GENRES)
            if 'cached_menu' in request.fixturenames:
                # Completed-menu consumers keep real cache SQL and files. SQL
                # handle admission is independent of their frozen-choice contract.
                request.getfixturevalue('native_sql_calls')
            store = Store(tmp_path / 'cached-home.db')
            resources.callback(store.conn.close)
            resources.enter_context(schema_inspection.reuse_tables(store.conn))
            store.conn.execute('PRAGMA synchronous=OFF')
            initialize_consumer(store, request.getfixturevalue('cached_menu_schema'))
            # Keep real WAL commits; checkpoint scheduling has its own owners
            # in core/test_publication_checkpoint, below these Home boundaries.
            store.conn.execute('PRAGMA wal_autocheckpoint=0')
            resources.enter_context(reuse_reporting_reader(store, monkeypatch))
        else:
            store = request.getfixturevalue('store')
            # Restore before other fixtures add listener state, feedback or jobs.
            if 'prepared' in request.fixturenames:
                store._fixture_home_prepared = request.getfixturevalue('home_prepared_template').restore(store)
            elif 'home_inputs' in request.fixturenames:
                request.getfixturevalue('home_seed_template').restore(store)
        yield store


@pytest.fixture
def catalog_import(store):
    """The original 24 untagged, unmodelled songs for the cold-start ranker."""
    tracks = [{'video_id': f'import-{i}', 'title': f'Song {i}', 'artist': f'Artist {i}',
               'key': f'song {i}|artist {i}'} for i in range(24)]
    seed_publication_subjects(store, tracks)
    return store


@pytest.fixture
def metadata_import(store):
    """Four genuinely imported songs for the metadata/publication race."""
    for i in range(4):
        store.tracks.upsert_track(f'import-{i}', f'Song {i}', f'Artist {i}', None, None)
    return store


@pytest.fixture
def home_inputs(store):
    """Private catalog/profile inputs; the test still performs Home preparation."""
    return store


@pytest.fixture
def prepared(store, home_prepared_template):
    if not hasattr(store, '_fixture_home_prepared'):
        store._fixture_home_prepared = home_prepared_template.restore(store)
    return store._fixture_home_prepared


@pytest.fixture
def cached_home_http(monkeypatch, home_routes_only):
    """Keep actual HTTP rendering without unrelated route/provider setup."""
    from fastapi import APIRouter
    from yt_playlist.providers import genres
    from yt_playlist.web.routes import home_artist_start, home_generation, home_growth, home_onboarding, home_tv

    monkeypatch.setattr(genres, 'configure', lambda *args, **kwargs: None)
    # Keep Home's actual document composition, card/control/discovery routes,
    # and middleware. These consumers do not request the other feature routes.
    for module in (home_artist_start, home_generation, home_growth, home_onboarding, home_tv):
        monkeypatch.setattr(module, 'build', lambda ctx: APIRouter())
    client_type = TestClient
    with ExitStack() as clients:
        monkeypatch.setattr(__name__ + '.TestClient',
            lambda *args, **kwargs: clients.enter_context(client_type(*args, **kwargs)))
        yield


@pytest.fixture
def cached_menu(store, monkeypatch):
    """Publish a completed worker menu below the consumer's test boundary.

    Ranking and preparation have their own owners. Keep four tracks per card,
    real subjects/modes and actual cache publication; serving still chooses,
    freezes and records its own assignment from this completed input.
    """
    with monkeypatch.context() as producer:
        inputs = controlled_publication_inputs(store, producer, tracks_per_card=4)
        result = inputs.cards(service(store), home_cards.CardsRequest(1000, epoch=0, record=False))
    home_prepared.remember(store, {'generation': 'controlled-worker-output'}, result, 1000)
    payload = home_cache.read(store, 'home_tuned')
    home_cache.put(store, home_prepared.SURFACE, payload, 1000)
    home_cache.put(store, 'home_tuned', None, 1000)
    home_cache.put(store, 'home_choice', {}, 1000)
    assert home_prepared._valid(payload)
    return payload


@pytest.fixture
def full_cached_menu(menu_publication_store, monkeypatch):
    """Completed twelve-track menus and shadows below serving/feedback."""
    store = menu_publication_store
    with monkeypatch.context() as producer:
        inputs = controlled_publication_inputs(store, producer, tracks_per_card=12)
        result = inputs.cards(service(store), home_cards.CardsRequest(1000, epoch=0, record=False))
    for proto in result.protos:
        proto['counterfactual'] = {'ranker': 'cosine', 'tracks': list(reversed(proto['tracks']))}
    home_prepared.remember(store, {'generation': 'controlled-worker-output'}, result, 1000)
    payload = home_cache.read(store, 'home_tuned')
    home_cache.put(store, home_prepared.SURFACE, payload, 1000)
    home_cache.put(store, 'home_tuned', None, 1000)
    home_cache.put(store, 'home_choice', {}, 1000)
    assert home_prepared._valid(payload)
    return store, payload


@pytest.fixture
def choice_menus(menu_publication_store, monkeypatch):
    """Completed alternatives below feedback-sensitive whole-menu selection.

    Keep twelve distinct four-card menus, all six actual modes and four tracks
    per card. Worker enumeration/ranking belongs to the preparation owners;
    this consumer still reads, scores and selects from actual persisted menus.
    """
    store = menu_publication_store
    menus = []
    with monkeypatch.context() as producer:
        inputs = controlled_publication_inputs(store, producer, tracks_per_card=4, stable_options=True)
        for epoch in range(home_prepared.MAX_MENUS):
            order = tuple((offset + epoch) % 6 for offset in range(6))
            if epoch >= 6:
                order = tuple(reversed(order))
            result = inputs.cards(service(store), home_cards.CardsRequest(
                1000, epoch=epoch, mode_order=order, record=False))
            home_prepared.remember(store, None, result, 1000)
            payload = home_cache.read(store, 'home_tuned')
            menus.append(payload['menus'][0])
    payload.update(menus=menus, modes=[
        {**mode, 'centroid': list(map(float, mode['centroid']))}
        for mode in store.modes.list_modes()])
    assert len({tuple(proto['experiment_id'] for proto in menu['protos'])
                for menu in menus}) == home_prepared.MAX_MENUS == 12
    assert all(len(menu['protos']) == 4 and all(len(proto['tracks']) == 4 for proto in menu['protos'])
               for menu in menus)
    assert home_prepared._valid(payload)
    home_cache.put(store, home_prepared.SURFACE, payload, 1000)
    home_cache.put(store, 'home_tuned', None, 1000)
    home_cache.put(store, 'home_choice', {}, 1000)
    return store, payload


@pytest.fixture
def publication_inputs(store, monkeypatch):
    return controlled_publication_inputs(store, monkeypatch)


@pytest.fixture
def menu_publication_inputs(uncontended_menu_store, monkeypatch):
    return controlled_publication_inputs(uncontended_menu_store, monkeypatch)


@pytest.fixture
def publication_prepared(publication_inputs, one_menu_per_attempt):
    payload = home_prepared.prepare_mixes(publication_inputs.store, 1000)
    assert home_prepared._valid(payload)
    return payload


@pytest.fixture
def one_menu_per_attempt(monkeypatch):
    """Exercise real setup/selection/publication once per worker slice.

    Alternative enumeration is covered by the twelve-attempt accumulation
    case. Dependency, failure and retry checks need one usable menu per attempt.
    Only Home's menu-slice clock advances; the actual work budget remains real.
    """
    clock = [0.0]
    original = home_cards.HomeCards.cards

    def cards(self, request):
        result = original(self, request)
        clock[0] += home_prepared.PREPARE_BUDGET_S * .6
        return result

    monkeypatch.setattr(home_prepared, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    return clock


def test_capacity_projection_preserves_worker_menu_after_feedback(uncontended_menu_store):
    store = uncontended_menu_store
    prepared = publish_menu(store, tracks_per_card=1)
    for proto in prepared['menus'][0]['protos']:
        proto['tracks'][0]['musical'] = {'captured_features': [0.2, 0.7, 0.1]}
    home_cache.put(store, home_prepared.SURFACE, prepared, 1000)

    def selections(reader, projected):
        payload = home_prepared.peek(reader, 1000, readiness_only=projected)
        assert all(('musical' in proto['tracks'][0]) is not projected
                   for proto in payload['menus'][0]['protos'])
        result = home_prepared.cards(reader, payload, 1000, service(reader), record=False,
                                     readiness_only=projected)
        return [[track['key'] for track in proto['tracks']] for proto in result.protos]

    with store.read_view() as reader:
        original = selections(reader, False)
        assert len(original) == 4
        assert selections(reader, True) == original
    seed_dislike(store, original[0][0], until=None, now=999)
    with store.read_view() as reader:
        assert selections(reader, True) == selections(reader, False) == original


def test_capacity_projection_does_not_hide_invalid_collection_data(store, cached_menu):
    from copy import deepcopy
    damaged = deepcopy(cached_menu)
    damaged['discovery_tracks'] = [{'key': 'incomplete'}]
    store.rec_proposals.put_proposals('home_tuned', damaged, 1000)
    with store.read_view() as reader:
        assert home_prepared.peek(reader, 1000, readiness_only=True)['tuning'] == cached_menu['tuning']
        assert home_prepared._load_surface(reader, 'home_tuned', readiness_only=True) is None


def test_replacement_context_preserves_metadata_without_copying_replaced_offers(store, cached_menu):
    with store.read_view() as reader:
        full = home_prepared.peek(reader, 1000)
        metadata = home_prepared.selection_context(reader, 1000)
        assert metadata == {key: value for key, value in full.items()
                            if key not in ('menus', 'album_choices', 'album_choice_context', 'play_exposure')}
        metadata['fingerprint']['breadth']['families']['changed'] = 99
        assert 'changed' not in home_prepared.selection_context(reader, 1000)['fingerprint']['breadth']['families']
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == cached_menu


def test_replacement_keeps_collection_fallback_without_restoring_its_profiles(store, cached_menu, monkeypatch):
    from yt_playlist.core import json_cache

    payload = deepcopy(cached_menu)
    track = {**payload['menus'][0]['protos'][0]['tracks'][0],
             'musical': {'captured': ['previous collection evidence'] * 20000}}
    payload['rediscover_tracks'] = [track]
    payload['discovery_tracks'] = [track]
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1000)
    restore = json_cache._restore_profiles

    def no_collection_profiles(value, profiles, *args, **kwargs):
        if isinstance(value, dict) and 'discovery_tracks' in value:
            assert all('musical' not in row for field in ('rediscover_tracks', 'discovery_tracks')
                       for row in value[field])
        return restore(value, profiles, *args, **kwargs)

    monkeypatch.setattr(json_cache, '_restore_profiles', no_collection_profiles)
    metadata = home_prepared.selection_context(store, 1000)
    expected = {key: child for key, child in track.items() if key != 'musical'}
    assert metadata['discovery_tracks'] == metadata['rediscover_tracks'] == [expected]
    assert metadata['choice_context'] == payload['choice_context']
    assert metadata['fingerprint'] == payload['fingerprint']
    monkeypatch.setattr(json_cache, '_restore_profiles', restore)
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == payload


def test_history_diagnostics_do_not_change_generation_but_effective_allocation_does(menu_publication_store):
    from yt_playlist.rec import choice_bandit, group_learning, group_serving
    store = menu_publication_store
    head = publish_groups(store, grouped_recordings([('Jazz', 20, .1), ('Techno', 20, .9)]), now=90)
    groups = [group for group in head['groups'] if group['leaf']]
    assert len(groups) == 2
    first, second = [group['group_id'] for group in groups]
    store.rec_proposals.put_proposals(group_serving.SURFACE, {
        'generation': head['generation'], 'groups': groups}, 90)
    stats = {'policy': group_learning.POLICY, 'generation': head['generation'],
             'parameters': choice_bandit.parameters(store), 'at': 90,
             'expires_at': 200, 'state_version': 'first',
             'rows': [{'group_id': first, 'estimate': .8}, {'group_id': second, 'estimate': .2}]}
    store.rec_proposals.put_proposals('group_choice_bandit', stats, 90)
    original = home_prepared._proposal_generation(store, None, 100)
    refreshed = {**stats, 'at': 101, 'offered': 99, 'state_version': 'new-diagnostics',
                 'rows': [{**row, 'offered': 99, 'menus': ['unanswered']} for row in stats['rows']]}
    store.rec_proposals.put_proposals('group_choice_bandit', refreshed, 101)
    assert home_prepared._proposal_generation(store, None, 102) == original
    changed = {**refreshed, 'rows': [{'group_id': first, 'estimate': .2}, {'group_id': second, 'estimate': .8}]}
    store.rec_proposals.put_proposals('group_choice_bandit', changed, 103)
    assert home_prepared._proposal_generation(store, None, 104) != original
    # Expiry and future inputs still invalidate a learned allocation.
    expired = home_prepared._proposal_generation(store, None, 201)
    assert expired != home_prepared._proposal_generation(store, None, 104)
    store.rec_proposals.put_proposals('group_choice_bandit', {**stats, 'at': 300}, 300)
    assert home_prepared._proposal_generation(store, None, 201) == expired
    for policy in (None, 'obsolete-policy'):
        invalid = {**stats, 'policy': policy}
        if policy is None:
            invalid.pop('policy')
        store.rec_proposals.put_proposals('group_choice_bandit', invalid, 90)
        assert home_prepared._proposal_generation(store, None, 100) == expired
    # Pool publication and changed reward configuration remain real inputs.
    store.rec_proposals.put_proposals('group_choice_bandit', stats, 90)
    fresh = [{'key': 'new candidate|artist', 'title': 'New candidate', 'artist': 'Artist',
              'video_id': 'new-candidate'}]
    seed_bundle_subjects(store, {'all': {}}, fresh=fresh)
    publish_bundles(store, {'all': {}}, now=105, fresh=fresh)
    changed_pool = home_prepared._proposal_generation(store, None, 106)
    assert changed_pool != original
    store.rec_proposals.put_proposals('group_choice_bandit', {
        **stats, 'parameters': {**stats['parameters'], 'window_d': -1}}, 105)
    assert home_prepared._proposal_generation(store, None, 106) != changed_pool


def test_generation_shares_play_counts_and_facets_across_summaries(
        publication_inputs, one_menu_per_attempt, monkeypatch):
    store = publication_inputs.store
    repo = type(store.rec_query)
    original = repo.play_counts
    calls = []
    proposal_repo = type(store.rec_proposals)
    read_proposals = proposal_repo.get_proposals
    bundle_reads = []

    def bounded_bundle_read(self, surface):
        if surface == 'mode_bundles':
            bundle_reads.append(self)
            raise AssertionError('Current Home must use typed candidate pools without decoding legacy bundles')
        return read_proposals(self, surface)

    def counted(self):
        calls.append(True)
        return original(self)

    def forbidden(*args, **kwargs):
        raise AssertionError('Home must reuse the generation summaries')

    monkeypatch.setattr(repo, 'play_counts', counted)
    monkeypatch.setattr(proposal_repo, 'get_proposals', bounded_bundle_read)
    monkeypatch.setattr(repo, 'genre_play_distribution', forbidden)
    monkeypatch.setattr(repo, 'era_play_distribution', forbidden)
    assert home_prepared.prepare(store, 1000) is not None
    assert len(calls) == 1
    assert not bundle_reads
    assert publication_inputs.requests
    assert all(not request.record for request in publication_inputs.requests)


def test_album_budget_deferral_still_publishes_mixes_with_last_good_album_context(
        publication_inputs, monkeypatch, one_menu_per_attempt):
    from yt_playlist.core.activity import WorkBudgetExceeded
    from yt_playlist.rec import album_choices
    from yt_playlist.rec.worker_tasks import ServingTasks
    store = publication_inputs.store
    albums = {'items': [{'browse_id': 'last-good', 'title': 'Retained album'}],
              'context': {'model': 'original album prediction'}}
    store.rec_proposals.put_proposals(album_choices.SURFACE, albums, 999)

    def exhausted(*args):
        raise WorkBudgetExceeded('Album preparation exceeded its independent budget')

    monkeypatch.setattr(album_choices, 'prepare', exhausted)
    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1000, client_provider=lambda: {},
        logger=logging.getLogger(__name__)), lambda name, target, *args, **kwargs: target(*args, **kwargs))
    assert tasks.home()  # Albums request another attempt through the existing Home backoff.
    result = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    assert result['menus'] and result['album_choices'] == albums['items']
    assert result['album_choice_context'] == albums['context']
    assert store.rec_proposals.get_proposals(album_choices.SURFACE) == albums


def test_reporting_failure_does_not_prevent_missing_mixes_from_preparing(
        publication_inputs, one_menu_per_attempt):
    from types import SimpleNamespace
    from yt_playlist.rec import choice_evidence
    from yt_playlist.rec.worker_tasks import ServingTasks

    store = publication_inputs.store
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) is None
    def stage(name, target, *args, **kwargs):
        if target is choice_evidence.refresh_report:
            raise OSError('Report publication failed')
        return target(*args, **kwargs)
    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1000,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), stage)
    with pytest.raises(OSError, match='Report publication failed'):
        tasks.home()
    payload = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    assert payload and payload['menus'][0]['protos']


def test_budget_retry_reuses_completed_genre_setup_and_publishes_a_menu(
        publication_inputs, monkeypatch, one_menu_per_attempt):
    from yt_playlist.core.activity import WorkBudgetExceeded
    store = publication_inputs.store
    original_options, original_cards = home_themes.genre_options, home_cards.HomeCards.cards
    options_calls, cards_calls = [], []

    def options(reader, **kwargs):
        options_calls.append(True)
        return original_options(reader, **kwargs)

    def cards(self, request):
        cards_calls.append(True)
        if len(cards_calls) == 1:
            raise WorkBudgetExceeded('The first menu starts after this batch deadline')
        return original_cards(self, request)

    monkeypatch.setattr(home_themes, 'genre_options', options)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    assert home_prepared.prepare(store, 1000) is None
    result = home_prepared.prepare(store, 1001)
    assert result and any(menu['protos'] for menu in result['menus'])
    assert len(options_calls) == 1


def test_slow_revisit_does_not_starve_first_mix_during_source_updates(
        publication_inputs, monkeypatch, one_menu_per_attempt):
    store = publication_inputs.store
    from yt_playlist.core.activity import WorkBudgetExceeded
    calls = []

    def delayed(reader, *args, **kwargs):
        calls.append(True)
        raise WorkBudgetExceeded('Large actual-history summary reached its separate deadline')

    monkeypatch.setattr(surfaces, 'rediscover_tracks', delayed)
    result = home_prepared.prepare(store, 1000)
    assert result and any(menu['protos'] for menu in result['menus'])
    assert calls and result['rediscover_tracks'] == []
    for now in (1061, 1122):
        store.settings.set_setting('last_sync_at', str(now))
        refreshed = home_prepared.prepare(store, now)
        assert refreshed and any(menu['protos'] for menu in refreshed['menus'])
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == refreshed


def test_served_whole_menu_probability_is_recorded_and_frozen(store, cached_menu):
    # Serving consumes completed worker alternatives. Actual enumeration and
    # accumulation remain covered by the bounded preparation case below.
    prepared = deepcopy(cached_menu)
    alternate = deepcopy(prepared['menus'][0])
    alternate['epoch'] += 3  # Both alternatives belong to the same choice band.
    alternate['protos'] = list(reversed(alternate['protos']))
    prepared['menus'].append(alternate)
    home_cache.put(store, home_prepared.SURFACE, prepared, 1000)
    first = home_prepared.cards(store, prepared, 1001, service(store))
    assignment = first.protos[0]["assignment"]
    assert len(assignment['candidates']) == 2
    assert {row['menu'] for row in assignment['candidates']} == {0, 3}
    assert sum(row["probability"] for row in assignment["candidates"]) == pytest.approx(1)
    assert 0 < assignment["probability"] <= 1
    assert assignment["unit"] == "whole_menu"
    again = home_prepared.cards(store, prepared, 1002, service(store))
    assert again.protos[0]["assignment"] == assignment
    assert [p["experiment_id"] for p in again.protos] == [p["experiment_id"] for p in first.protos]
    from yt_playlist.rec import choice_evidence
    options = [{'id': p['experiment_id'], 'position': i + 1, 'label': p['label'],
                'note': p['note'], 'intent': p['lane'], 'genres': p.get('genres', []),
                'assignment': p['assignment']} for i, p in enumerate(first.protos)]
    offered = choice_evidence.present(store, 'mixes', options, 1001,
        context=first.choice_context or prepared['choice_context'], window=first.epoch)
    held = choice_evidence.opportunity(store, offered)
    assert held and all(option['assignment'] == assignment for option in held['options'])
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 0


def test_missing_candidate_dependencies_keep_last_usable_home(store, cached_menu):
    prepared = cached_menu
    tracks = prepared['menus'][0]['protos'][0]['tracks']
    publish_bundles(store, {'all': {'explore': tracks}, '_meta': {'unmodeled': True}}, fresh=tracks)
    assert {row['surface'] for row in store.recommendations.heads()} >= {'home', 'fresh'}
    with store.conn:
        store.conn.execute("DELETE FROM recommendation_heads WHERE surface IN ('home','fresh')")
    assert home_prepared.prepare(store, 1001) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared
    assert home_prepared.cards(store, prepared, 1001, service(store), record=False).protos


def test_new_skip_keeps_the_prepared_menu_and_the_heart(store, cached_menu):
    prepared = cached_menu
    first = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    track = next(track for proto in first.protos for track in proto['tracks'])
    iid = store.accounts.upsert_account('Main', 'credential', None, True)
    seed_like(store, track['key'], 1001)
    store.player_events.record_player_event(
        iid, 'next', track['video_id'], 1, 200, '', None, 1002)
    served = home_prepared.cards(store, prepared, 1003, service(store), record=False)
    assert served.protos
    assert [p['tracks'] for p in served.protos] == [p['tracks'] for p in first.protos]
    assert track['key'] in store.rec_model.recent_liked_keys()
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


def test_initial_import_can_offer_catalog_mix_before_tags_or_models(catalog_import, monkeypatch, one_menu_per_attempt):
    store = catalog_import
    entry_id = store.tracks.track_ids_for_videos(['import-0'])['import-0']
    calculate = mode_surfaces._calculate_bundles

    def enrich_during_calculation(reader, now, policies):
        result = calculate(reader, now, policies)
        store.enrichment.apply_provider_fields(entry_id, 'musicbrainz',
            {'genre': 'House'}, providers=['musicbrainz'])
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', enrich_during_calculation)
    bundles = mode_surfaces.prepare_bundles(store, 1000)
    assert bundles and bundles['all']['explore']
    assert not store.modes.list_modes(active_only=True)
    assert not store.rec_model.rec_vectors_count()

    def forbidden(*args, **kwargs):
        raise AssertionError('Home assembly and serving must only consume prepared rankings')

    monkeypatch.setattr(surfaces, 'explore_for_you', forbidden)
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    prepared = home_prepared.prepare(store, 1001)
    assert home_prepared._valid(prepared)
    result = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    catalog = next(p for p in result.protos if p['lane'] == 'explore')
    assert len(catalog['tracks']) >= 4
    assert catalog.get('mode_id') is None
    disliked = catalog['tracks'][0]['key']
    seed_dislike(store, disliked, until=None, now=1002)
    result = home_prepared.cards(store, prepared, 1002, service(store), record=False)
    assert next(p for p in result.protos if p['lane'] == 'explore')['tracks'] == catalog['tracks']


def test_explicit_metadata_choice_during_build_allows_captured_candidate_heads_to_finish(metadata_import, monkeypatch):
    from yt_playlist.core import catalog

    store = metadata_import
    entries = [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]
    assert len(entries) == 4
    assert mode_surfaces.prepare_bundles(store, 1000)
    before = {surface: store.recommendations.head(surface)['id'] for surface in ('home', 'fresh')}
    calculate = mode_surfaces._calculate_bundles

    def choose_during_calculation(reader, now, policies):
        result = calculate(reader, now, policies)
        with store.conn:
            catalog.choose_field(store.conn, entries[0], 'genre', 'House', 'user', now=now)
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', choose_during_calculation)
    assert mode_surfaces.prepare_bundles(store, 1001) is not None
    assert {surface: store.recommendations.head(surface)['id'] for surface in ('home', 'fresh')} != before
    assert store.tracks.track_detail('import-0')['genre'] == 'House'


def test_changed_artist_links_do_not_rewrite_the_worker_publication(
        publication_inputs, one_menu_per_attempt, monkeypatch):
    store = publication_inputs.store
    store.rec_model.record_feedback('for_you', 'artist:Guest', 'mute', now=999)
    original = home_cards.HomeCards.cards
    corrected = []

    def correct_credit(self, request):
        result = original(self, request)
        if not corrected:
            proto = next(p for p in result.protos if p['lane'] != 'fresh')
            track = proto['tracks'][0]
            corrected.append(deepcopy(proto))
            store.tracks.upsert_track('imported', 'Imported song', 'New artist', '', None)
            # A changed canonical credit after selection belongs to the next
            # worker preparation, rather than a second selection during serving.
            store.tracks.upsert_track(track['video_id'], track['title'], track['artist'], '', None,
                                      credits=[{'name': 'Guest'}])
        return result

    monkeypatch.setattr(home_cards.HomeCards, 'cards', correct_credit)
    prepared = home_prepared.prepare(store, 1000)
    assert prepared is not None
    key = corrected[0]['tracks'][0]['key']
    assert key in {t['key'] for m in prepared['menus'] for p in m['protos'] for t in p['tracks']}
    current = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    chosen = next(menu for menu in prepared['menus']
                  if [p['experiment_id'] for p in menu['protos']] == [p['experiment_id'] for p in current.protos])
    assert [p['tracks'] for p in current.protos] == [p['tracks'] for p in chosen['protos']]


@pytest.mark.parametrize('method,path', [
    ('get', '/'), ('get', '/home/cards'), ('get', '/home/feed'),
    ('get', '/home/discovery'), ('get', '/home/genres'),
    ('post', '/home/refresh-cards'), ('post', '/home/refresh-card/wheelhouse'),
], ids=['page', 'cards', 'feed', 'discovery', 'genres', 'refresh', 'refresh-wheelhouse'])
def test_ordinary_home_reads_use_prepared_results_without_selection_or_analytics(
        store, publication_prepared, monkeypatch, method, path):
    from yt_playlist.rec import account_growth, growth_readiness

    prepared = publication_prepared
    # These routes consume readiness; computing and publishing every preceding
    # product is not part of their serving contract. The real progression gates
    # and their output requirements are owned by test_growth_journey and
    # test_growth_readiness. Keep actual prepared menus and HTTP rendering here.
    monkeypatch.setattr(growth_readiness, 'foundation', lambda *a: True)
    monkeypatch.setattr(growth_readiness, 'albums', lambda *a: True)
    monkeypatch.setattr(growth_readiness, 'discover', lambda *a: True)
    from fastapi import APIRouter, Response
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, home

    rendered = []
    templates = webapp.Jinja2Templates

    def serving_templates(*args, **kwargs):
        result = templates(*args, **kwargs)

        def render(request, name, context, **options):
            rendered.append((request.url.path, name, context))
            return Response(media_type='text/html', **options)

        # This case owns serving without expensive producers/analytics. Keep
        # HTTP, views and actual prepared selections; rendered markup retains
        # its owners in test_mode_cards_route and web/test_home_composition.
        monkeypatch.setattr(result, 'TemplateResponse', render)
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', serving_templates)
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [app_update.build(ctx), home.build(ctx)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *a: None)
    # Force the description/audit fallback on actual selected tracks. A fully
    # tagged fixture used to miss its whole-library artist lookup.
    for menu in prepared['menus']:
        for proto in menu['protos']:
            proto['tracks'][0]['genre'] = ''
    store.rec_proposals.put_proposals(home_prepared.SURFACE, prepared, 1000)
    state = account_growth.view(store, 1000)
    assert state['ready_formats']['mixes']
    assert state['ready_areas']['discover'] and state['ready_areas']['album-choices']
    def forbidden(*a, **kw):
        raise AssertionError("Heavy Home work must stay in background preparation")
    for target, name in ((mode_surfaces, "assemble_cards"), (home_themes, "genre_options"),
                         (surfaces, "for_you"), (surfaces, "rediscover_playlists"),
                         (surfaces, "rediscover_albums")):
        monkeypatch.setattr(target, name, forbidden)
    for name in ("play_counts", "track_audio_features", "genre_play_distribution", "era_play_distribution",
                 "artist_genre_years"):
        monkeypatch.setattr(type(store.rec_query), name, forbidden)
    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.settings.set_setting("last_sync_at", "1000")
    store.settings.set_setting("onboard_dismissed", "1")
    # Each route independently consumes the same completed publication. A
    # previous route's reads or choice writes must not be its precondition.
    with closing(TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1001),
                            base_url="http://127.0.0.1")) as client:
        response = getattr(client, method)(path)
        assert response.status_code == 200
        if path in ("/", "/home/cards", "/home/feed"):
            rendered_path, _, context = rendered[-1]
            assert rendered_path == path
            assert not context['preparing']
            assert {proto['lane'] for proto in context['protos']} == {
                'wheelhouse', 'explore', 'comfort', 'fresh'}
            assert all(proto['tracks'] for proto in context['protos'])


def test_genre_preference_edits_schedule_and_publish_a_new_prepared_generation(store, publication_prepared):
    prepared = publication_prepared
    scheduled = []
    store.rec_model.set_weight('genre:techno', 0.5, now=1000)
    retained = home_prepared.load(store, 1000, lambda: scheduled.append(True))
    assert retained['tuning'] == prepared['tuning']
    assert scheduled == [True]
    refreshed = home_prepared.prepare(store, 1000)
    assert refreshed is not None
    assert refreshed['tuning'] != prepared['tuning']
    assert home_prepared.load(store, 1000)['tuning'] == refreshed['tuning']
    store.rec_model.reset_weights()
    assert home_prepared.tuning_key(store, 1000) == prepared['tuning']


@pytest.mark.parametrize('damage', ['experiment_id', 'rediscover'])
def test_corrupt_prepared_payload_never_breaks_home_routes(store, cached_menu, damage, cached_home_http):
    payload = deepcopy(cached_menu)
    if damage == 'experiment_id':
        payload['menus'][0]['protos'][0].pop('experiment_id')
    else:
        payload['rediscover'] = None
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1000)
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1000')
    store.settings.set_setting('onboard_dismissed', '1')
    returning_listener(store, 'mixes', now=1000)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1001)
    scheduled = []
    app.state.ctx.rec_worker.trigger_home_refresh = lambda **kw: scheduled.append(True)
    client = TestClient(app, base_url='http://127.0.0.1')
    for path in ('/', '/home/cards', '/home/feed', '/home/discovery', '/home/genres'):
        assert client.get(path).status_code == 200
    assert scheduled


def test_saving_fresh_tracks_in_generated_playlists_does_not_truncate_worker_mixes(full_cached_menu, monkeypatch):
    store, prepared = full_cached_menu
    initial = home_prepared.cards(store, prepared, 1000, service(store))
    proto = next(p for p in initial.protos if p['lane'] == 'fresh')
    assert len(proto['tracks']) == 12
    assert proto['counterfactual']['tracks'] == list(reversed(proto['tracks']))
    original = deepcopy(prepared)
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(iid, 'saved-mix', 'Saved mix', 7, None, 1001)
    tids = [store.tracks.upsert_track(track['video_id'], track['title'], track['artist'], '', None)
            for track in proto['tracks'][:7]]
    store.playlists.set_playlist_tracks(pid, tids)
    store.playlists.mark_generated_playlist('saved-mix')
    assert {t['key'] for t in proto['tracks'][:7]} <= store.rec_eligibility.generated_only_keys()

    def forbidden(*args, **kwargs):
        raise AssertionError('Serving must not reselect or revalidate a worker-produced mix')

    monkeypatch.setattr(mode_surfaces, 'assemble_cards', forbidden)
    monkeypatch.setattr(surfaces, 'for_you', forbidden)

    def serve(now):
        # Catch revalidation on private readers as well as the serving writer.
        # Feedback setup and the explicit eligibility checks below stay real.
        with monkeypatch.context() as serving:
            serving.setattr(type(store.rec_query), 'tracks_by_keys', forbidden)
            serving.setattr(type(store.rec_eligibility), 'offer_metadata', forbidden)
            serving.setattr(type(store.discovery), 'discovered_tracks_by_keys', forbidden)
            return home_prepared.cards(store, prepared, now, service(store))

    current = serve(1002)
    assert len(current.protos) == len(current.slots) == 4
    assert {p['lane']: p['tracks'] for p in current.protos} == {
        p['lane']: p['tracks'] for p in initial.protos}
    assert all(not (p.get('assignment') or {}).get('censored') for p in current.protos)
    keys = [t['key'] for t in proto['tracks']]
    assert not store.rec_eligibility.suppressed_candidate_keys(keys, 1002)
    # Feedback changes future preparation, never the frozen tracks or shadows.
    seed_dislike(store, keys[0], until=None, now=1003)
    disliked = serve(1003)
    assert [p['tracks'] for p in disliked.protos] == [p['tracks'] for p in initial.protos]
    assert [p.get('counterfactual') for p in disliked.protos] == [p.get('counterfactual') for p in initial.protos]
    assert prepared == original


def test_cached_playlist_uses_current_title_membership_and_eligibility(store, cached_menu):
    prepared = cached_menu
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(iid, 'old-list', 'Old title', 5, None, 1000)
    # The prepared teaser deliberately names tracks no longer in the playlist.
    prepared['rediscover'] = [{'id': pid, 'title': 'Old title', 'track_count': 5,
                               'tracks': [{'title': 'Removed teaser'}]}]
    store.rec_proposals.put_proposals(home_prepared.REVISIT_SURFACE,
                                     {'rediscover': prepared['rediscover']}, 1000)
    tid = store.tracks.upsert_track('current', 'Current teaser', 'Current artist', None, None)
    store.playlists.set_playlist_tracks(pid, [tid])
    store.playlists.upsert_playlist(iid, 'old-list', 'New title', 1, 'new', 1001)
    current = home_prepared.collections(store, prepared, 1001)['rediscover']
    assert current[0]['title'] == 'New title' and current[0]['track_count'] == 1
    assert [t['title'] for t in current[0]['tracks']] == ['Current teaser']
    store.playlists.set_playlist_tracks(pid, [])
    assert home_prepared.collections(store, prepared, 1001)['rediscover'] == []
    with store.conn:
        store.conn.execute('UPDATE playlists SET title=?,track_count=0 WHERE id=?', ('Empty now', pid))
    assert home_prepared.collections(store, prepared, 1001)['rediscover'] == []


def test_previews_and_repeated_fragments_do_not_inflate_exposure(store, cached_menu):
    prepared = cached_menu
    home_prepared.cards(store, prepared, 1000, service(store), record=False)
    assert store.modes.impression_counts() == {}
    home_prepared.cards(store, prepared, 1000, service(store))
    counts = {t["identity_key"]: t["offered_count"] for t in store.discovery.get_discovered_tracks()}
    impressions = store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0]
    home_prepared.cards(store, prepared, 1001, service(store))
    assert {t["identity_key"]: t["offered_count"] for t in store.discovery.get_discovered_tracks()} == counts
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == impressions


def test_hold_window_keeps_the_choice_through_refill_and_new_evidence(store, cached_menu):
    prepared = deepcopy(cached_menu)
    prepared['modes'] = [{key: mode[key] for key in ('mode_id', 'size')}
                         for mode in store.modes.list_modes(active_only=True)]
    prepared['choice_context'] = {'at': 999, 'generation': 'original-context'}
    tuning = home_prepared.tuning_key(store, 1000)
    ranking = candidate_refresh.preferences(store, 1000)
    first = home_prepared.cards(store, prepared, 1000, service(store))
    assert not first.preparing
    assert home_prepared.tuning_key(store, 1000) == tuning
    assert candidate_refresh.preferences(store, 1000) != ranking
    ids = {p["experiment_id"] for p in first.protos}
    replacement = deepcopy(prepared)
    replacement['choice_context'] = {'at': 1001, 'generation': 'later-context'}
    for mode in replacement["modes"]:
        mode["size"] = 100000 if mode["mode_id"] not in {p.get("mode_id") for p in first.protos} else 1
    for epoch in range(20, 40):
        store.modes.log_impressions(epoch, [(str(mid), mid) for mid in range(6)], 1001)
    store.rec_ledger.bump_card_view('cards', 1001)
    assert home_prepared.tuning_key(store, 1002) == tuning
    held = home_prepared.cards(store, replacement, 1002, service(store))
    assert not held.preparing
    assert {p["experiment_id"] for p in held.protos} == ids
    # Rebuilding the pool with entirely different menus must not silently reassign
    # the held decision while retaining the old propensity.
    replacement['menus'] = [deepcopy(replacement['menus'][0])]
    for proto in replacement['menus'][0]['protos']:
        proto['experiment_id'] = 'replacement-' + proto['experiment_id']
    after_refill = home_prepared.cards(store, replacement, 1003, service(store))
    assert {p['experiment_id'] for p in after_refill.protos} == ids
    assert after_refill.protos[0]['assignment'] == first.protos[0]['assignment']
    assert after_refill.choice_context == first.choice_context == prepared['choice_context']


@pytest.mark.parametrize('state', ('presync', 'onboarding'))
def test_presync_previews_and_onboarding_menus_are_not_exposures(store, cached_menu, state, cached_home_http):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    if state == 'onboarding':
        store.modes.replace_modes([], list(range(6)), 1000)
        # Genuine onboarding has neither retained playable mixes nor prior
        # completed setup. The presync case retains output but no earned access.
        with store.conn.atomic():
            from yt_playlist.core import home_outputs
            home_outputs.clear(store.conn)
            store.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('home_prepared', 'home_tuned')")
        store.settings.set_setting("last_sync_at", "1000")
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000),
                        base_url="http://127.0.0.1")
    response = client.get("/")
    assert response.status_code == 200
    # Prepared output alone does not skip the fresh listener's first stop.
    assert 'id="onboarding-feed"' in response.text
    assert 'id="home-feed"' not in response.text
    assert store.modes.impression_counts() == {}
    assert store.rec_ledger.scorecard_inputs(0, 2000)[0] == []
    assert all(t["offered_count"] == 0 for t in store.discovery.get_discovered_tracks())


def test_visible_prepared_home_without_modes_records_exposures(store, cached_menu, cached_home_http):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.modes.replace_modes([], list(range(6)), 1000)
    store.settings.set_setting("last_sync_at", "1000")
    returning_listener(store, 'mixes', now=1000)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000),
                        base_url="http://127.0.0.1")

    response = client.get("/")
    assert response.status_code == 200
    assert 'id="home-feed"' in response.text
    assert 'id="onboarding-feed"' not in response.text
    assert 'data-preparing="false"' in response.text
    shown = home_prepared.cards(store, cached_menu, 1000, service(store), record=False).protos
    assert len(shown) == 4
    expected = {(p["lane"], t["key"]) for p in shown for t in p["tracks"]}
    assert all(f'data-key="{key}"' in response.text for _, key in expected)
    assert store.modes.impression_counts() == Counter(p["mode_id"] for p in shown)
    offers = store.rec_ledger.evidence().records('opportunity')
    assert len(offers) == 1 and offers[0]['format'] == 'mixes'
    assert len(offers[0]['options']) == 4
    assert store.rec_ledger.scorecard_inputs(0, 2000)[0] == []
    offered = {t["identity_key"]: t["offered_count"] for t in store.discovery.get_discovered_tracks()}
    fresh = {key for lane, key in expected if lane == "fresh"}
    assert fresh
    # A displayed mix exposes its theme, not each song it might later play.
    assert offered == {key: 0 for key in offered}
    held = store.rec_proposals.get_proposals('home_choice')
    for path in ('/home/cards', '/home/feed', '/'):
        repeated = client.get(path)
        assert repeated.status_code == 200
        assert 'data-preparing="false"' in repeated.text
        assert all(f'data-key="{key}"' in repeated.text for _, key in expected)
        assert store.rec_proposals.get_proposals('home_choice') == held


@pytest.mark.parametrize("bad", [None, {}, {"schema": 999}, "bad-json"])
def test_missing_or_corrupt_cache_queues_preparation_without_ranking(store, monkeypatch, bad):
    monkeypatch.setattr(home_cache, 'read', lambda reader, key: bad if key == home_prepared.SURFACE else None)
    calls = []
    assert home_prepared.load(store, 1000, lambda: calls.append(True)) is None
    result = home_prepared.cards(store, None, 1000, service(store), lambda: calls.append(True))
    assert result.protos == []
    assert len(result.slots) == 4 and result.preparing
    assert calls


def test_malformed_menu_is_discarded_and_damaged_tuned_cache_keeps_baseline(store, cached_menu, monkeypatch):
    prepared = cached_menu
    corrupt = deepcopy(prepared)
    corrupt["menus"][0]["protos"] = [None]
    assert not home_prepared._valid(corrupt)
    original = home_cache.read
    def get(reader, surface):
        if surface == "home_tuned":
            raise ValueError("corrupt JSON")
        return original(reader, surface)
    monkeypatch.setattr(home_cache, 'read', get)
    assert home_prepared.load(store, 1001) == prepared


def test_feedback_and_removed_sources_do_not_rewrite_worker_mixes(store, cached_menu):
    prepared = cached_menu
    initial = home_prepared.cards(store, prepared, 1000, service(store), record=False)
    assert initial.protos
    key = initial.protos[0]["tracks"][0]["key"]
    seed_dislike(store, key, until=None, now=1001)
    other = initial.protos[0]["tracks"][1]["key"]
    with store.conn:
        store.conn.execute("DELETE FROM library_memberships WHERE entry_id IN "
                           "(SELECT e.id FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id "
                           "WHERE r.identity_key=?)", (other,))
    result = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    assert [p['tracks'] for p in result.protos] == [p['tracks'] for p in initial.protos]


def test_worker_produced_empty_slot_stays_clickable_and_polls_for_a_replacement(store, cached_menu, cached_home_http):
    cached_menu['menus'] = [cached_menu['menus'][0]]
    menu = cached_menu['menus'][0]
    menu['protos'] = [p for p in menu['protos'] if p['lane'] != 'wheelhouse']
    menu['slots'][0] = {'proto': None, 'pool': service(store).pool_view({
        'lane': 'wheelhouse', 'epoch': menu['epoch'], 'include_genres': [], 'avoid_genres': []},
        genre_options=cached_menu['genre_options'])}
    store.rec_proposals.put_proposals(home_prepared.SURFACE, cached_menu, 1000)
    calls = []
    result = home_prepared.cards(store, cached_menu, 1001, service(store), lambda: calls.append(True))
    assert len(result.protos) == 3 and len(result.slots) == 4
    assert result.slots[0]['proto'] is None
    assert result.slots[0]['pool']['lane'] == 'wheelhouse'
    assert result.slots[0]['pool']['parameters'] == {
        'lane': 'wheelhouse', 'epoch': result.epoch, 'include_genres': [], 'avoid_genres': []}
    assert calls
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    returning_listener(store, 'mixes', now=1000)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1001),
                        base_url='http://127.0.0.1')
    response = client.get('/home/feed')
    assert response.status_code == 200
    assert 'id="gen-wheelhouse"' in response.text
    assert 'hx-post="/home/library-pool"' in response.text
    assert 'hx-get="/home/cards?prepare_attempt=1"' in response.text
    assert 'model keeps learning' not in response.text
    assert 'id="gen-more"' not in response.text


@pytest.mark.parametrize('cached_placeholders,attempt,next_attempt,interval', [
    (False, 0, 1, 3), (True, 999, 10, 30),
])
def test_startup_cards_keep_polling_with_backoff_until_offers_recover(
        store, cached_menu, monkeypatch, attempt, next_attempt, interval, cached_placeholders, cached_home_http):
    returning_listener(store, 'mixes', now=1000)
    client = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000,
                                   background_work=False), base_url='http://127.0.0.1')
    pending = None
    if cached_placeholders:
        pending = deepcopy(cached_menu)
        menu = pending['menus'][0]
        menu['protos'] = []
        menu['slots'] = [{'proto': None, 'pool': service(store).pool_view(
            {'include_genres': [], 'avoid_genres': [], **({'lane': lane, 'epoch': 0} if lane != 'fresh' else {})},
            genre_options=pending['genre_options'])} for lane in ('wheelhouse', 'explore', 'fresh', 'comfort')]
        pending['menus'] = [menu]
        # No other healthy cache exists during this startup. Serving now checks
        # retained surfaces as well as the payload returned by load().
        store.rec_proposals.put_proposals(home_prepared.SURFACE, pending, 1000)
    monkeypatch.setattr(home_prepared, 'load', lambda *args, **kwargs: pending)
    response = client.get(f'/home/cards?prepare_attempt={attempt}')
    assert response.status_code == 200
    assert f'hx-get="/home/cards?prepare_attempt={next_attempt}"' in response.text
    assert f'hx-trigger="every {interval}s"' in response.text
    assert 'hx-post="/home/library-pool"' in response.text

    # A later worker publication terminates polling and replaces the pending
    # slots with playable offers, even after the previous ten-attempt limit.
    store.rec_proposals.put_proposals(home_prepared.SURFACE, cached_menu, 1000)
    monkeypatch.setattr(home_prepared, 'load', lambda *args, **kwargs: cached_menu)
    recovered = client.get(f'/home/cards?prepare_attempt={next_attempt}')
    assert recovered.status_code == 200
    assert 'hx-get="/home/cards?prepare_attempt=' not in recovered.text
    assert 'data-key=' in recovered.text
    if cached_placeholders:
        assignment = store.rec_proposals.get_proposals('home_choice')['assignment']
        assert assignment['generation'] == cached_menu['generation']
        assert not assignment.get('censored')


def test_cached_discovery_honors_unavailability_and_current_tile_metadata(store, publication_prepared):
    prepared = publication_prepared
    initial = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    fresh = next(p for p in initial.protos if p["lane"] == "fresh")
    key = fresh["tracks"][0]["key"]
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE recording_id IN "
                           "(SELECT id FROM recordings WHERE identity_key=?)", (key,))
    cards = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    assert [p['tracks'] for p in cards.protos] == [p['tracks'] for p in initial.protos]
    store.discovery.upsert_discovered_album("new-album", "Artist", "Album", "2020", None, 1000, genre="Jazz")
    tiles = home_prepared.prepare(store, 1001)
    assert tiles["discovery_albums"]
    store.discovery.upsert_discovered_album("new-album", "Artist", "Album", "2020", None, 1002,
                                           genre="Techno")
    store.rec_model.set_weight("genre:techno", 0, lo=0, now=1002)
    assert home_prepared.discovery(store, tiles, 1002)["discovery_albums"] == []


def test_feedback_changes_the_next_choice_and_tracks_can_repeat(choice_menus):
    store, prepared = choice_menus
    assert len(prepared['menus']) == home_prepared.MAX_MENUS
    first = home_prepared.cards(store, prepared, 1000, service(store), record=False)
    first_modes = {p["mode_id"] for p in first.protos if p.get("mode_id") is not None}
    # Strong exposure without picks is evidence against repeating the same themes.
    for epoch in range(20, 70):
        store.modes.log_impressions(epoch, [(str(mid), mid) for mid in first_modes], 500)
    second = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    second_modes = {p["mode_id"] for p in second.protos if p.get("mode_id") is not None}
    assert second_modes - first_modes
    a = {t["key"] for p in first.protos for t in p["tracks"]}
    b = {t["key"] for p in second.protos for t in p["tracks"]}
    assert a & b  # Reuse of constituent tracks remains valid.


def test_cache_rebuild_and_restart_preserve_evidence_and_experiment_identity(
        uncontended_menu_store, menu_publication_inputs, one_menu_per_attempt):
    # Identity needs a displayed offer and real rebuild, below independent rankers.
    store = uncontended_menu_store
    path = store._db_path
    try:
        before = home_prepared.prepare_mixes(store, 1000)
        assert home_prepared._valid(before)
        offer = home_prepared.cards(store, before, 1000, service(store))
        stats = store.modes.impression_counts()
        ids = {p["experiment_id"] for p in offer.protos}
        assert stats and ids
        rebuilt = home_prepared.prepare(store, 1001)
        assert rebuilt and rebuilt['built_at'] == 1001
        assert len(rebuilt['menus']) == min(home_prepared.MAX_MENUS, len(before['menus']) + 1)
        assert rebuilt['next_menu'] == (before['next_menu'] + 1) % home_prepared.MAX_MENUS
        assert store.modes.impression_counts() == stats
    finally:
        store.conn.close()
    reopened = Store(path)
    try:
        reopened.conn.execute('PRAGMA synchronous=OFF')
        # Keep actual startup and retrieval of committed menus and evidence.
        reopened.init_schema()
        after = home_prepared.load(reopened, 1002)
        assert after and reopened.modes.impression_counts() == stats
        assert after == rebuilt
        assert ids <= {p["experiment_id"] for m in after["menus"] for p in m["protos"]}
    finally:
        reopened.conn.close()


def test_generation_race_retains_previous_cache(store, publication_prepared, monkeypatch):
    prepared = publication_prepared
    tid = store.tracks.track_ids_for_videos(['v-wheelhouse-0-0'])['v-wheelhouse-0-0']
    store.tracks.set_track_genre(tid, 'House')  # Recompute the completed genre setup for new metadata.
    original = home_themes.genre_options
    def changed(reader, **kwargs):
        result = original(reader, **kwargs)
        seed_dislike(store, "x", until=None, now=1002)
        return result
    monkeypatch.setattr(home_themes, "genre_options", changed)
    assert home_prepared.prepare(store, 1002) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


def test_setup_sql_obeys_total_budget_and_preserves_previous_cache(store, prepared, monkeypatch):
    import sqlite3

    tid = store.tracks.track_ids_for_videos(['v-wheelhouse-0-0'])['v-wheelhouse-0-0']
    store.tracks.set_track_genre(tid, 'House')
    make_reader = store._make_reader
    readers, interrupted = [], []

    def reader():
        value = make_reader()
        readers.append(value.conn)
        return value

    def slow_setup():
        try:
            store.conn.execute('WITH RECURSIVE slow(n) AS (VALUES(0) UNION ALL '
                'SELECT n+1 FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow').fetchone()
        except sqlite3.OperationalError as exc:
            assert str(exc) == 'interrupted'
            interrupted.append(True)
            raise

    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 0.2)
    monkeypatch.setattr(store, '_make_reader', reader)
    monkeypatch.setattr(store._snapshot_revisions, 'refresh', slow_setup)
    # Discovery and Revisit have their own summary budgets. Measure the mix
    # stage, with real SQL during revision capture, so cold decoding
    # cannot consume the deadline before this cancellation control is reached.
    started = time.monotonic()
    assert home_prepared.prepare_mixes(store, 1002) is None
    assert time.monotonic() - started < 1.5
    assert len(readers) == len(interrupted) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        readers[0].execute('SELECT 1')
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


@pytest.mark.parametrize('boundary', ['before_commit', 'reader_cleanup'])
@pytest.mark.parametrize('reason', ['deadline', 'shutdown'])
def test_publication_acknowledges_commit_across_cleanup_deadline(
        store, publication_prepared, monkeypatch, boundary, reason):
    from yt_playlist.core import activity

    prepared = publication_prepared
    clock, stopping = [100.], [False]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(home_prepared, 'prepare_discovery', lambda *args: None)
    monkeypatch.setattr(home_prepared, 'prepare_revisit', lambda *args: None)

    def expire():
        if reason == 'shutdown':
            stopping[0] = True
        clock[0] += home_prepared.PREPARE_BUDGET_S + 1

    if boundary == 'reader_cleanup':
        snapshot = store.read_snapshot

        @contextmanager
        def late_cleanup(**kwargs):
            with snapshot(**kwargs) as view:
                yield view
            # The real publication and reader cleanup have completed. Its
            # payload must survive the calculation scope's final deadline check.
            expire()

        monkeypatch.setattr(store, 'read_snapshot', late_cleanup)
    else:
        publish = home_cache.publish

        def expire_before_commit(*args, **kwargs):
            publish(*args, **kwargs)
            # SQL has run inside the real publication transaction; the final
            # cancellation check must still roll it back before commit.
            expire()

        monkeypatch.setattr(home_cache, 'publish', expire_before_commit)

    result = None
    if reason == 'shutdown':
        with pytest.raises(activity.WorkClosing), activity.cancellable_work(lambda: stopping[0]):
            home_prepared.prepare(store, 1002)
    else:
        result = home_prepared.prepare(store, 1002)
    stored = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    if boundary == 'before_commit':
        assert stored == prepared
        assert result is None
    else:
        assert stored['built_at'] == 1002
        assert stored != prepared
        if reason == 'deadline':
            assert result == stored


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_small_preparation_batches_accumulate_alternatives_across_attempts(publication_inputs, monkeypatch):
    """Complete every alternative through bounded retries without any exposure."""
    store = publication_inputs.store
    # No preference changes occur during these composition retries. Read the
    # actual completed controls once below the retry/publication boundary.
    parameters = {spec.name: rec_params.get_param(store, spec.name) for spec in rec_params.PARAMS}
    monkeypatch.setattr(rec_params, 'get_param', lambda reader, name: parameters[name])
    # Catalog/modes remain fixed across these retries. Options are a completed
    # lower input to retry composition; retain their real first construction.
    options = service(store).options()
    monkeypatch.setattr(home_cards.HomeCards, 'options', lambda self: deepcopy(options))
    clock = [100]
    original = home_cards.HomeCards.cards

    def one_menu(self, request):
        result = original(self, request)
        clock[0] += 6
        return result

    monkeypatch.setattr(home_prepared, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 10)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', one_menu)
    for size in range(1, home_prepared.MAX_MENUS + 1):
        # Independent discovery/revisit summaries are not part of mix retries.
        payload = home_prepared.prepare_mixes(store, 1000 + size)
        assert payload is not None
        assert len(payload['menus']) == size
        # The next attempt must consume the previous committed document, not
        # merely a detached Python value from the calculation above.
        assert home_cache.read(store, home_prepared.SURFACE) == payload
    assert len(payload['menus']) == home_prepared.MAX_MENUS
    assert [request.epoch for request in publication_inputs.requests] == list(range(home_prepared.MAX_MENUS))
    assert all(not request.record for request in publication_inputs.requests)
    assert all(menu['protos'] for menu in payload['menus'])
    assert {p['mode_id'] for m in payload['menus'] for p in m['protos']
            if p.get('mode_id') is not None} == set(range(6))
    assert store.modes.impression_counts() == {}
    assert store.conn.execute('SELECT COUNT(*) FROM rec_served_impressions').fetchone()[0] == 0
    assert all(track['offered_count'] == 0 for track in store.discovery.get_discovered_tracks())
    assert all(proto['recipe']['experiment_id'] == proto['experiment_id']
               for menu in payload['menus'] for proto in menu['protos'])


def test_hydrated_menus_accumulate_losslessly_and_serve_after_reopen(uncontended_menu_store, monkeypatch):
    from yt_playlist.core import home_outputs, json_cache, policy_config
    from yt_playlist.rec import musical_character

    store = uncontended_menu_store
    path = store._db_path
    # The producer's catalog/modes stay fixed across all twelve alternatives.
    # Resolve their options once below the real accumulation/file boundary.
    controlled_publication_inputs(store, monkeypatch, stable_options=True)
    policy, base = policy_config.pack({'schema': 4, 'ranker': 'controlled-menu'}, 1000)
    store.rec_ledger.record_prepared_policies([policy], [base])
    clock, expected = [100.], {}
    original = home_cards.HomeCards.cards

    def hydrate(value):
        if isinstance(value, dict):
            if isinstance(value.get('musical'), dict):
                # Numeric features and an opaque captured source exercise both
                # profile preservation and a logical document over four MB.
                value['musical']['captured_features'] = {
                    f'feature-{i}': (i + 1) / 137 for i in range(3)}
                value['musical']['captured_source'] = 'fixture-source-observation|' * 800
            for item in value.values():
                hydrate(item)
        elif isinstance(value, list):
            for item in value:
                hydrate(item)

    def one_hydrated_menu(self, request):
        result = original(self, request)
        for proto in result.protos:
            for track in proto['tracks']:
                track['pool_policy'] = policy[0]
                track['musical'] = {'version': musical_character.VERSION,
                    'dimensions': {'intensity': {'value': .7, 'support': 1}}}
            proto['counterfactual'] = {'tracks': deepcopy(proto['tracks'])}
        hydrate(result.protos)
        expected[result.epoch] = deepcopy(result.protos)
        clock[0] += .9
        return result

    monkeypatch.setattr(home_prepared, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(home_cards.HomeCards, 'cards', one_hydrated_menu)
    try:
        # Two real file/SQL publications exercise lossless continuation. The
        # small retry owner above checks each of the twelve cursor transitions.
        for size in (6, home_prepared.MAX_MENUS):
            payload = home_prepared.prepare_mixes(store, 1000)
            assert payload is not None and len(payload['menus']) == size
            assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == payload
        assert home_prepared.MAX_MENUS == 12 and home_prepared.MAX_BYTES == 4_000_000
        assert len(json.dumps(payload, separators=(',', ':')).encode()) > home_prepared.MAX_BYTES
        raw = store.rec_proposals._proposal_json(home_prepared.SURFACE)
        assert len(raw.encode()) <= home_prepared.MAX_BYTES
        assert json.loads(raw)[json_cache.MARKER] == json_cache.FORMAT
        for menu in payload['menus']:
            for proto, before in zip(menu['protos'], expected[menu['epoch']], strict=True):
                assert proto['tracks'] == before['tracks']
                assert proto.get('counterfactual') == before.get('counterfactual')
                assert proto['experiment_id'] == home_cards.experiment_id(proto)
                assert proto['recipe']['experiment_id'] == proto['experiment_id']
            assert all(slot['proto'] in menu['protos'] for slot in menu['slots'] if slot['proto'])
        assert store.modes.impression_counts() == {}
        assert home_outputs.policy_references(payload) == {policy[0]}
        assert {row[0] for row in store.conn.execute('SELECT raw_key FROM home_display_policies')} >= (
            home_outputs.policy_references(payload))
    finally:
        store.conn.close()

    reopened = Store(path)
    # Match ordinary disk startup: admit the real schema and revision guards
    # without auditing unrelated metadata before serving the retained menu.
    reopened.init_schema(defer_metadata=True)
    try:
        assert home_prepared.load(reopened, 1001) == payload
        offer = home_prepared.cards(reopened, payload, 1001, service(reopened), record=False)
        assert offer.protos
        menu = next(menu for menu in payload['menus'] if menu['epoch'] == offer.protos[0]['assignment']['menu'])
        assert [proto['tracks'] for proto in offer.protos] == [proto['tracks'] for proto in menu['protos']]
    finally:
        reopened.conn.close()


def test_failed_menu_file_replacement_preserves_last_good_publication(
        menu_publication_inputs, one_menu_per_attempt, monkeypatch):
    # Failure retention does not require twelve multi-megabyte alternatives.
    # The large-document owner above checks exact accumulation and disk reopen;
    # test_capacity_projection_preserves_worker_menu_after_feedback owns feedback.
    store = menu_publication_inputs.store
    previous = home_prepared.prepare_mixes(store, 1000)
    assert previous and previous['menus'][0]['protos']
    original = home_cache.encoded(store, home_prepared.SURFACE)

    def fail(*args, **kwargs):
        raise OSError('Controlled output-volume failure')

    monkeypatch.setattr(store.home_cache.files, 'write_cache', fail)
    with pytest.raises(OSError, match='output-volume'):
        home_prepared.prepare_mixes(store, 1001)
    assert home_cache.encoded(store, home_prepared.SURFACE) == original
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == previous


def test_preparation_can_publish_while_ordinary_unrelated_ui_writes_continue(
        store, publication_prepared, monkeypatch):
    tid = store.tracks.track_ids_for_videos(['v-wheelhouse-0-0'])['v-wheelhouse-0-0']
    store.tracks.set_track_genre(tid, 'House')
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    original = home_themes.genre_options

    def with_ui_writes(reader, **kwargs):
        options = original(reader, **kwargs)
        # These writes continue while source catch-up is pending. Neither changes
        # the consumed model generation or the selected candidates' eligibility.
        store.settings.set_setting('last_sync_at', '1002')
        store.settings.set_setting('home_tab', 'mixes')
        return options

    monkeypatch.setattr(home_themes, 'genre_options', with_ui_writes)
    assert home_prepared.prepare(store, 1002) is not None
    assert store.rec_refresh.has_pending_changes()  # publication did not wait for the source backlog to drain


def test_automatic_refreshes_share_backoff_and_retry_without_another_request(store, monkeypatch):
    from yt_playlist.rec import rec_worker

    now = [10]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 1000), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    monkeypatch.setattr(home_prepared, 'prepare_mixes', lambda *a: None)
    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', lambda *a: {})
    worker.trigger_home_refresh()
    worker._run_pending()
    assert worker.jobs.retry['home'].dirty and not worker.jobs.has('home')
    for _ in range(16):
        now[0] += 1
        worker._refresh_bundles()
        worker._retry_home()
        assert not worker.jobs.has('home')
    now[0] = 40
    worker._retry_home()
    assert worker.jobs.has('home')
    worker.shutdown()


def test_explicit_result_replaces_held_menu_without_reusing_its_random_assignment(store, cached_menu):
    first = home_prepared.cards(store, cached_menu, 1000, service(store))
    assert first.protos[0]['assignment']['probability'] > 0
    explicit = deepcopy(first)
    explicit.protos[0]['tracks'].reverse()
    explicit.protos[0]['name'] = 'Explicitly rebuilt mix'
    home_prepared.remember(store, cached_menu, explicit, 1001)
    # A later refill of the disposable pool must not resurrect the previous offer.
    home_cache.put(store, 'home_tuned', cached_menu, 1002)
    again = home_prepared.cards(store, home_prepared.load(store, 1002), 1002, service(store))
    assert again.protos[0]['name'] == 'Explicitly rebuilt mix'
    assert again.protos[0]['tracks'] == explicit.protos[0]['tracks']
    assert [p['tracks'] for p in again.protos[1:]] == [p['tracks'] for p in first.protos[1:]]
    assert all(p['assignment'] is None for p in again.protos)
    assert again.choice_context == first.choice_context


def test_explicit_preview_round_trip_preserves_tracks_and_choice_context(
        store, cached_menu, monkeypatch):
    first = home_prepared.cards(store, cached_menu, 1000, service(store))
    held = store.rec_proposals.get_proposals('home_choice')
    invalid = deepcopy(first)
    invalid.protos[0]['counterfactual'] = {'tracks': [object()]}
    with pytest.raises(TypeError, match='not JSON serializable'):
        home_prepared.remember(store, cached_menu, invalid, 1000)
    assert store.rec_proposals.get_proposals('home_tuned') is None
    assert store.rec_proposals.get_proposals('home_choice') == held
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == cached_menu

    previous = first.protos[0]
    pool = []
    for index, track in enumerate(previous['tracks']):
        item = surfaces.ForYouItem(
            track['title'], track['artist'], track.get('album', ''), track['video_id'],
            track.get('thumbnail'), track.get('plays', 0), 'Preview selection',
            key=track['key'], lane=previous['lane'], genre=track.get('genre', ''),
            year=track.get('year'), trace={'ranker': {'score': .75}}, score=.75)
        pool.append(asdict(item) if index % 2 else item)
    # Use the real preview producer, which supports both track representations.
    proto = home_cards._carded(store, previous['lane'], previous['label'], pool, 1000)
    assert len(proto['tracks']) >= 4
    assert any(isinstance(track, surfaces.ForYouItem) for track in proto['tracks'])
    assert any(isinstance(track, dict) for track in proto['tracks'])
    proto['counterfactual'] = {'ranker': 'cosine', 'tracks': list(reversed(proto['tracks']))}
    proto['assignment'] = previous['assignment']
    proto['experiment'] = {'assignment': previous['assignment'], 'ranker_controlled': True}
    produced_tracks = tuple(proto['tracks'])
    expected_tracks = [asdict(track) if isinstance(track, surfaces.ForYouItem) else deepcopy(track)
                       for track in proto['tracks']]
    explicit = deepcopy(first)
    explicit.protos[0] = proto
    explicit.choice_context = {**first.choice_context, 'comparison': {'previous_menu': True}}
    home_prepared.remember(store, cached_menu, explicit, 1000)

    tuned = store.rec_proposals.get_proposals('home_tuned')
    held = store.rec_proposals.get_proposals('home_choice')
    assert home_prepared._valid(tuned)
    cached = tuned['menus'][0]['protos'][0]
    assert cached == held['menu']['protos'][0] == held['menu']['slots'][0]['proto']
    assert cached['tracks'] == expected_tracks
    assert cached['counterfactual']['tracks'] == list(reversed(expected_tracks))
    assert cached['recipe']['experiment_id'] == home_cards.experiment_id(proto)
    assert cached['assignment'] is cached['experiment']['assignment'] is None
    assert cached['experiment']['ranker_controlled'] is False
    assert held['choice_context'] == tuned['choice_context'] == first.choice_context
    assert held['assignment'] is None
    # Serializing the preview must not replace the producer's live track objects.
    assert all(track is original for track, original in zip(proto['tracks'], produced_tracks, strict=True))

    def no_inline_ranking(*args, **kwargs):
        raise AssertionError('Ordinary cached reads must not rebuild candidates')

    monkeypatch.setattr(home_cards.HomeCards, 'cards', no_inline_ranking)
    again = home_prepared.cards(store, home_prepared.load(store, 1001), 1001, service(store))
    assert again.protos[0]['tracks'] == expected_tracks
    assert again.protos[0]['counterfactual']['tracks'] == list(reversed(expected_tracks))
    assert again.choice_context == first.choice_context
    seed_dislike(store, expected_tracks[0]['key'], until=None, now=1001)
    served = home_prepared.cards(store, home_prepared.load(store, 1001), 1001, service(store))
    assert served.protos[0]['tracks'] == expected_tracks
    assert served.protos[0]['counterfactual']['tracks'] == list(reversed(expected_tracks))


def test_tuned_result_is_reused_and_expiry_restores_baseline(menu_publication_store):
    store = menu_publication_store
    prepared = publish_menu(store, genre='jazz', modeled=True)
    with mix_tuning.adjusting(store, 1000):
        store.rec_model.set_lean("genre:jazz", 2, 1000)
    # Cache reuse consumes a completed explicit result. Ranking under leans
    # has its own HomeCards owners; keep real subjects, publication and expiry.
    result = home_cards.CardsResult(**deepcopy(prepared['menus'][0]))
    result.protos[0]['tracks'].reverse()
    assert result.protos[0]['tracks'] != prepared['menus'][0]['protos'][0]['tracks']
    home_prepared.remember(store, prepared, result, 1000)
    tuned = home_prepared.load(store, 1001)
    assert tuned["tuning"] == home_prepared.tuning_key(store, 1000)
    assert tuned['tuning'] != prepared['tuning']
    assert len(tuned["menus"]) == 1
    assert tuned['menus'][0]['protos'][0]['tracks'] == result.protos[0]['tracks']
    expired = home_prepared.load(store, 1000 + mix_tuning.TTL_S)
    assert expired["tuning"] == prepared["tuning"]
    assert expired['menus'] == prepared['menus']
    assert store.rec_model.get_leans() == {}


def test_cold_requests_coalesce_home_work_without_requesting_a_model_fit(store, monkeypatch):
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 1000), debounce_s=0)
    launches = []
    monkeypatch.setattr(worker, "_start_loop", lambda: launches.append(True))
    for _ in range(10):
        worker.trigger_home_refresh()
    assert launches == [True] and worker.jobs.has('home')
    assert not worker.jobs.has('full') and not worker.jobs.has('modes')
    worker.shutdown()


@pytest.fixture
def shutdown_sql_store(tmp_path, cached_menu_schema):
    # This owner reaches an actual cancellable recursive query before ranking.
    # Completed candidate/menu construction belongs to the other Home owners.
    # Keep instrumented handle admission, fresh readers and real revision guards.
    with _file_consumer_store(tmp_path, cached_menu_schema) as database:
        yield database


def test_ui_remains_available_during_real_preparation_sql_and_shutdown_interrupts(
        shutdown_sql_store, monkeypatch):
    store = shutdown_sql_store
    started = threading.Event()
    def slow(reader, **kwargs):
        assert reader.conn is not store.conn
        reader.conn.create_function("started", 0, lambda: (started.set(), 0)[1])
        reader.conn.execute("WITH RECURSIVE slow(n) AS (VALUES(started()) UNION ALL SELECT n+1 "
                            "FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow").fetchone()
    monkeypatch.setattr(home_themes, "genre_options", slow)
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 1000), debounce_s=0)
    def interact():
        release = store.priority.enter()
        try:
            store.settings.set_setting("ui", "responsive")
            return store.settings.get_setting("ui")
        finally:
            release()
    try:
        worker.trigger_home_refresh()
        assert started.wait(5)
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(interact).result(2) == "responsive"
        worker.shutdown()
        worker.join(3)
    finally:
        worker.shutdown()
        worker.join(5)
        store.conn.close()
