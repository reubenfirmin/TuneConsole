"""Selected prepared reads retain candidate order and current eligibility."""
from copy import deepcopy
from contextlib import contextmanager
from functools import wraps
from tests.derived_rating_support import seed_dislike
import json
import sqlite3
import time

import pytest

from yt_playlist.core import bundle_plan, fao, json_cache
from tests.recommendation_support import publish_bundles, seed_bundle_subjects, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, uncontended_file_consumer_store as store
from yt_playlist.rec import home_vibe, measurement, musical_character, musical_profiles, vibe_pools
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.core.proposal_codec import encode_proposals


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Prepared SQL/file readers do not start the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Keep real typed ranks, immutable profiles and current eligibility below
    # both selectors. Taxonomy bootstrap is independent of these three groups.
    with bounded_consumer_schema((('techno', 'Techno'), ('jazz', 'Jazz'),
                                  ('house', 'House')), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture(autouse=True)
def query_diagnostics(monkeypatch):
    """Native private SQL below selectors; handle admission has other owners.

    Reads, rank paging, files, head invalidation and feedback stay real. These
    synchronous inputs contain no concurrent writer or snapshot conflict.
    """
    from yt_playlist.core.db_diagnostics import DiagnosticConnection

    for operation in ('execute', 'executemany'):
        monkeypatch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))


@contextmanager
def measured(costs, phase):
    if costs is None:
        yield
        return
    wall, cpu = time.perf_counter(), time.thread_time()
    try:
        yield
    finally:
        value = costs.setdefault(phase, [0, 0., 0.])
        value[0] += 1
        value[1] += time.perf_counter() - wall
        value[2] += time.thread_time() - cpu


def timed(costs, phase, operation):
    @wraps(operation)
    def call(*args, **kwargs):
        with measured(costs, phase):
            return operation(*args, **kwargs)
    return call


@contextmanager
def seed_costs(store, costs):
    """Observe real transaction finishes and durable writes without replacing them."""
    if costs is None:
        yield
        return
    owner = type(store.conn)
    finish = owner._finish

    def transaction(conn, sql, operation, *args):
        if conn is not store.conn:
            return finish(conn, sql, operation, *args)
        with measured(costs, 'subject_seed_' + sql.lower()):
            return finish(conn, sql, operation, *args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(owner, '_finish', transaction)
        patch.setattr(fao, '_write', timed(costs, 'subject_seed_artifact_write', fao._write))
        yield


@pytest.fixture
def vibe_read_costs(record_property):
    costs = {}
    yield costs
    for phase, (calls, wall, cpu) in costs.items():
        record_property(phase + '_calls', calls)
        record_property(phase + '_wall_s', wall)
        record_property(phase + '_thread_cpu_s', cpu)


def bundles(*, count=200, reserve_count=12):
    def items(preset, lane, family='general', count=count):
        return [{'key': f'{preset}:{lane}:{family}:{i}', 'video_id': f'v-{preset}-{lane}-{family}-{i}',
                 'title': f'{preset} {lane} {family} Song {i}', 'artist': f'Artist {i}',
                 'genre': 'Techno' if i % 2 else 'Jazz', 'score': i / 100,
                 'musical': {'schema': 1, 'version': musical_profiles.VERSION,
                             'dimensions': {'intensity': {'value': i / count, 'support': .8}},
                             'provenance': 'complete retained evidence ' * 30}}
                for i in range(count)]
    return {'all': {'fresh': items('global', 'fresh')}, '_meta': {'complete': True},
            '_vibes': {'version': home_vibe.VERSION,
                'pools': {preset: {lane: items(preset, lane) for lane in ('fresh', 'explore')}
                          for preset in ('energize', 'relax')},
                'genres': {preset: {lane: {family: items(preset, lane, family, reserve_count)
                                           for family in ('techno', 'jazz')}
                                    for lane in ('fresh', 'explore')}
                           for preset in ('energize', 'relax')}}}


def encoded_bundles(value, format):
    if format == 'plain':
        encoded = json.dumps(value)
    elif format == 'v1':
        encoded = json.dumps({json_cache.MARKER: json_cache.LEGACY_FORMAT,
                              'json': json_cache._pack(json.dumps(value))})
    else:
        encoded = encode_proposals('mode_bundles', value)
        assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    return encoded


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
def test_private_bundle_codecs_preserve_complete_prepared_values(format):
    value = bundles()
    assert json_cache.decode(encoded_bundles(value, format)) == value


def publish(store, value, *, costs=None):
    # Current reads consume the same typed publication for every private input
    # encoding. Exercise codec parity above without repeating database setup.
    value = value or {}
    prepared = value.get('_vibes') or {}
    items = [item for lanes in prepared.get('pools', {}).values() for rows in lanes.values() for item in rows]
    items += [item for lanes in prepared.get('genres', {}).values() for groups in lanes.values()
              for rows in groups.values() for item in rows]
    # These are inputs to one publication. Keep every real repository write,
    # but let the supported outer owner commit the complete seed once instead
    # of finishing one transaction per discovered recording. File syncs remain.
    with measured(costs, 'subject_seed'), seed_costs(store, costs), store._lock, store.conn.atomic():
        seed_bundle_subjects(store, value, fresh=items)
    profiles = {item['key']: item['musical'] for item in items}
    with measured(costs, 'profile_publication'):
        store.musical_profiles.publish_recordings([(key, musical_profiles.canonical(profile), profile['version'],
            musical_profiles.digest(profile)) for key, profile in profiles.items()], 1)
    with measured(costs, 'typed_publication'):
        publish_bundles(store, value, 1)


@pytest.fixture
def selected_vibe_inputs(store, monkeypatch, vibe_read_costs):
    # Cross native rank pages/chunks with complete genre reserves, without a
    # prepared database export or a second inventory of private file copies.
    value = bundles(count=4, reserve_count=2)
    prepared = value['_vibes']
    items = [item for lanes in prepared['pools'].values() for rows in lanes.values() for item in rows]
    items += [item for lanes in prepared['genres'].values() for groups in lanes.values()
              for rows in groups.values() for item in rows]
    for item in [*value['all']['fresh'], *items]:
        item['reason'] = 'Prepared context for ' + item['key']
    with measured(vibe_read_costs, 'subject_seed'):
        seed_publication_subjects(store, [*value['all']['fresh'], *items],
                                  discoveries=[item['key'] for item in items])
    profiles = {item['key']: item['musical'] for item in items}
    with measured(vibe_read_costs, 'profile_publication'):
        store.musical_profiles.publish_recordings([
            (key, musical_profiles.canonical(profile), profile['version'], musical_profiles.digest(profile))
            for key, profile in profiles.items()], 1)
    monkeypatch.setattr(vibe_pools, 'POOL_LIMIT', 2)
    monkeypatch.setattr(bundle_plan, 'EXPLANATION_BYTES', 128)
    monkeypatch.setattr(measurement, 'serving_policy',
                        lambda *args, **kwargs: ('vibe-read-fixture', {'fixture': 'selected-vibe-read'}))
    with measured(vibe_read_costs, 'typed_publication'):
        publish_bundles(store, value, 1)
    assert store.conn.execute('SELECT count(*) FROM prepared_outputs WHERE first_rank>0').fetchone()[0] > 0
    return value


def full_decode(store, *, page_size=128):
    """Reference: hydrate every preset, lane and genre before selecting one."""
    result = {'_vibes': {'version': home_vibe.VERSION, 'pools': {}, 'genres': {}}}
    with store.read_view() as reader:
        for preset in ('energize', 'relax'):
            surface = 'vibe:' + preset
            head = reader.recommendations.head(surface)
            for lane in ('fresh', 'explore'):
                for slot in reader.recommendations.selections(surface, lane, generation=head['id']):
                    mode, after, rows = slot['mode_key'], -1, []
                    while True:
                        page = reader.recommendations.candidate_page(surface, mode, lane,
                            generation=head['id'], after=after, limit=page_size)
                        profiles = musical_character.load(reader, [item['key'] for item in page['tracks']])
                        rows.extend({**item, 'musical': profiles[item['key']]} for item in page['tracks'])
                        after = page['after']
                        if page['exhausted']:
                            break
                    target = result['_vibes']['genres' if mode else 'pools'].setdefault(preset, {})
                    if mode:
                        target.setdefault(lane, {})[mode] = rows
                    else:
                        target[lane] = rows
    return result


def test_preparation_preserves_raw_genre_groups_and_refreshes_taxonomy(store):
    from yt_playlist.rec import measurement

    with store.conn:
        store.conn.executemany(
            "INSERT INTO genre_catalog(key,name,family_key,source) VALUES (?,?,?,'custom')", [
                ('fixture-old', 'Retired fixture style', 'house'),
                ('fixture-current', 'Current fixture style', 'techno'),
                ('fixture-left', 'Shared fixture label', 'jazz'),
                ('fixture-right', 'Other fixture label', 'house'),
            ])
        store.conn.execute("INSERT INTO genre_redirects VALUES ('fixture-old','fixture-current')")
        store.conn.execute("INSERT INTO genre_alias VALUES ('shared fixture label','Shared fixture label',"
                           "'fixture-right','custom')")
    labels = ['Retired fixture style', 'fixture-old', 'Current fixture style',
              'Shared fixture label', 'Novel fixture style', 'House', '', None]
    rows = [{'key': f'genre-input-{i}', 'video_id': f'genre-video-{i}', 'title': f'Song {i}',
             'artist': f'Artist {i}', 'genre': label, 'year': (1990, 2005, 2020)[i % 3],
             'score': i / 10} for i, label in enumerate(labels)]
    lanes = ('wheelhouse', 'explore', 'comfort', 'fresh', 'temporal')
    original = {'all': {lane: deepcopy(rows) for lane in lanes},
                '_meta': {'year_cuts': [2000, 2010]}}
    seed_bundle_subjects(store, original)

    def prepare(now):
        with store.read_snapshot() as snapshot:
            return vibe_pools.prepare(snapshot.store, deepcopy(original), now,
                                      measurement.PoolPolicies(snapshot.store, now))

    def assert_groups(value, expected):
        prepared = value['_vibes']
        assert set(prepared['pools']) == set(prepared['genres']) == set(home_vibe.PRESETS)
        for preset in home_vibe.PRESETS:
            for lane in (*lanes[:-1], 'temporal:0', 'temporal:1', 'temporal:2'):
                wanted = [i for i in range(len(rows))
                          if not lane.startswith('temporal:') or i % 3 == int(lane[-1])]
                assert {item['key'] for item in prepared['pools'][preset][lane]} == {
                    rows[i]['key'] for i in wanted}
                groups = {}
                for i in wanted:
                    if expected[i]:
                        groups.setdefault(expected[i], set()).add(rows[i]['key'])
                actual = prepared['genres'][preset][lane]
                assert list(actual) == list(groups)
                assert {family: {item['key'] for item in items}
                        for family, items in actual.items()} == groups
                for items in actual.values():
                    for item in items:
                        assert item['genre'] == labels[int(item['key'].rsplit('-', 1)[1])]
                        assert item['pool_policy']

    first = prepare(10)
    first_expected = ['techno', 'techno', 'techno', 'other:shared fixture label',
                      'other:novel fixture style', 'house', '', '']
    assert_groups(first, first_expected)
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='fixture-current'")
        store.conn.execute("DELETE FROM genre_alias WHERE label_key='shared fixture label'")
        store.conn.execute("INSERT INTO genre_catalog(key,name,family_key,source) VALUES "
                           "('fixture-new','Novel fixture style','house','custom')")
    second = prepare(11)
    assert_groups(second, ['jazz', 'jazz', 'jazz', 'jazz', 'house', 'house', '', ''])
    assert_groups(first, first_expected)  # The prior build remains detached.
    assert original['all'] == {lane: rows for lane in lanes}


def test_selected_vibe_read_matches_full_decode_and_skips_unrelated_profiles(
        store, monkeypatch, selected_vibe_inputs, vibe_read_costs):
    with measured(vibe_read_costs, 'full_decode'):
        decoded = full_decode(store, page_size=2)
    load = musical_character.load
    # Both lanes consume one immutable publication and one full-decode oracle.
    # Keep all eight selection scenarios and a fresh spy for each bounded read,
    # without republishing identical subjects/files or decoding both lanes twice.
    for lane in ('fresh', 'explore'):
        for include, avoid in [((), ()), (('techno',), ()), ((), ('jazz',)),
                               (('jazz', 'techno'), ('techno',))]:
            with measured(vibe_read_costs, 'full_decode_selection'):
                expected = vibe_pools.candidates(store, 'energize', lane, bundles=decoded,
                                                include_genres=include, avoid_genres=avoid)
            visited = []

            def selected(reader, keys):
                visited.extend(keys)
                # Selected rank pages now share one load; the profile repository
                # still enforces its own 512-identity pages inside this helper.
                assert len(keys) <= 16 * vibe_pools.POOL_LIMIT
                for key in keys:
                    preset, selected_lane, family, _ = key.split(':')
                    assert preset == 'energize' and selected_lane == lane
                    if family != 'general':
                        assert (include or avoid) and family not in avoid and (not include or family in include)
                return load(reader, keys)

            with measured(vibe_read_costs, 'selected_read'), monkeypatch.context() as scenario:
                scenario.setattr(musical_character, 'load', selected)
                assert vibe_pools.candidates(store, 'energize', lane,
                    include_genres=include, avoid_genres=avoid) == expected, (lane, include, avoid)
            assert len(expected) <= vibe_pools.POOL_LIMIT
            assert visited, (lane, include, avoid)
            if include or avoid:
                # Filtering must traverse beyond the first selected native page.
                assert any(int(key.rsplit(':', 1)[1]) >= 2 for key in visited), (lane, include, avoid)




def test_vibe_candidate_retry_is_detached_invalidated_and_rechecks_dislikes(store, monkeypatch, vibe_read_costs):
    # This case inserts discovery records before publication and later replaces
    # its head. Thirteen rows leave a replacement after a twelve-track pick;
    # unrelated presets, lanes and genre reserves belong to the selected-read
    # parity case above, which crosses real pages with a controlled page size.
    value = bundles()
    pool = value['_vibes']['pools']['energize']['fresh'][:13]
    value['all'] = {}
    value['_vibes']['pools'] = {'energize': {'fresh': pool}}
    value['_vibes']['genres'] = {}
    items = value['_vibes']['pools']['energize']['fresh'][:12]
    with measured(vibe_read_costs, 'retry_existing_subject_seed'), store._lock, store.conn.atomic():
        for item in items:
            store.discovery.upsert_discovered_track(item['key'], item['video_id'], item['title'],
                item['artist'], '', None, item['genre'], None, None, 1)
    with measured(vibe_read_costs, 'retry_initial_publication'):
        publish(store, value, costs=vibe_read_costs)
    original_json = RecommendationProposals._proposal_json
    def no_legacy_bundle(repo, surface, *args, **kwargs):
        if surface == 'mode_bundles':
            raise AssertionError('Selected typed reads must not inflate the retired global bundle')
        return original_json(repo, surface, *args, **kwargs)

    monkeypatch.setattr(RecommendationProposals, '_proposal_json', no_legacy_bundle)
    with measured(vibe_read_costs, 'retry_initial_read'), store.read_snapshot() as snapshot:
        first = vibe_pools.candidates(snapshot.store, 'energize', 'fresh')
        first[0]['musical']['dimensions']['intensity']['value'] = -1
        selected = vibe_pools.pick(snapshot.store, 1000, 'energize', 'fresh')
    assert selected
    key = selected[0]['key']
    with measured(vibe_read_costs, 'retry_dislike_write'):
        seed_dislike(store, key, until=None, now=1001)
    with measured(vibe_read_costs, 'retry_disliked_read'), store.read_snapshot() as snapshot:
        second = vibe_pools.candidates(snapshot.store, 'energize', 'fresh')
        assert second[0]['musical']['dimensions']['intensity']['value'] >= 0
        assert key not in {item['key'] for item in vibe_pools.pick(snapshot.store, 1002, 'energize', 'fresh')}
    replacement = deepcopy(value)
    replacement['_vibes']['pools']['energize']['fresh'].reverse()
    expected = replacement['_vibes']['pools']['energize']['fresh'][0]['key']
    previous_head = store.recommendations.head('vibe:energize')['id']
    with measured(vibe_read_costs, 'retry_replacement_publication'):
        publish(store, replacement, costs=vibe_read_costs)
    assert store.recommendations.head('vibe:energize')['id'] != previous_head
    with measured(vibe_read_costs, 'retry_replacement_read'), store.read_snapshot() as snapshot:
        assert vibe_pools.candidates(snapshot.store, 'energize', 'fresh')[0]['key'] == expected


@pytest.mark.parametrize('value', [None, {}, {'_vibes': {'version': 'older-preset-policy'}}])
def test_missing_or_older_vibe_candidates_remain_empty(store, value):
    publish(store, value)
    assert vibe_pools.candidates(store, 'energize', 'fresh') == []
