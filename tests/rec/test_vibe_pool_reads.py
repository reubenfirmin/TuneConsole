"""Selected prepared reads retain candidate order and current eligibility."""
from copy import deepcopy
from contextlib import ExitStack, contextmanager
from functools import wraps
from tests.derived_rating_support import seed_dislike
import json
import time

import pytest

from yt_playlist.core import fao, json_cache
from yt_playlist.core import profile_files
from tests import prepared_fixture_support
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_bundle_subjects
from yt_playlist.rec import home_vibe, musical_character, musical_profiles, vibe_pools
from yt_playlist.repos.rec_proposals import RecProposalsRepo, encode_proposals


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
        patch.setattr(fao, '_sync', timed(costs, 'subject_seed_directory_sync', fao._sync))
        yield


@pytest.fixture(scope='module')
def selected_vibe_costs():
    return {}


@pytest.fixture
def vibe_read_costs(record_property, selected_vibe_costs):
    costs = {}
    yield costs
    # Cold construction belongs in the file total, even when later reads reuse
    # its template. Inclusive timers overlap and must not be added together.
    if selected_vibe_costs and not selected_vibe_costs.get('_reported'):
        for phase, (calls, wall, cpu) in selected_vibe_costs.items():
            record_property('cold_' + phase + '_calls', calls)
            record_property('cold_' + phase + '_wall_s', wall)
            record_property('cold_' + phase + '_thread_cpu_s', cpu)
        selected_vibe_costs['_reported'] = True
    for phase, (calls, wall, cpu) in costs.items():
        record_property(phase + '_calls', calls)
        record_property(phase + '_wall_s', wall)
        record_property(phase + '_thread_cpu_s', cpu)


def bundles():
    def items(preset, lane, family='general', count=200):
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
                'genres': {preset: {lane: {family: items(preset, lane, family, 12)
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
    with measured(costs, 'profile_publication'), store._lock, store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.musical_profiles.publish_recordings([(key, musical_profiles.canonical(profile), profile['version'],
            musical_profiles.digest(profile)) for key, profile in profiles.items()], 1)
    with measured(costs, 'typed_publication'):
        publish_bundles(store, value, 1)


@pytest.fixture(scope='module')
def selected_vibe_template(_pristine_current_schema, selected_vibe_costs):
    def build(source):
        value = bundles()
        publish(source, value, costs=selected_vibe_costs)
        return value

    with ExitStack() as resources:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(prepared_fixture_support, 'capture_template', timed(
                selected_vibe_costs, 'template_export', prepared_fixture_support.capture_template))
            with measured(selected_vibe_costs, 'template_initialization_build_export'):
                template = resources.enter_context(prepared_template(_pristine_current_schema, build))
        yield template


@pytest.fixture
def selected_vibe_inputs(store, selected_vibe_template, vibe_read_costs):
    with measured(vibe_read_costs, 'private_restore'):
        return selected_vibe_template.restore(store)


def full_decode(store):
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
                            generation=head['id'], after=after, limit=128)
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


@pytest.mark.parametrize('lane', ['fresh', 'explore'])
def test_selected_vibe_read_matches_full_decode_and_skips_unrelated_profiles(
        store, monkeypatch, lane, selected_vibe_inputs, vibe_read_costs):
    with measured(vibe_read_costs, 'full_decode'):
        decoded = full_decode(store)
    load = musical_character.load
    # Each scenario reads the same immutable publication. Keep both lanes and
    # every filter combination, with a fresh spy for each bounded read.
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


def test_selected_vibe_template_retains_complete_private_publications(
        store, file_backed_store, selected_vibe_template, selected_vibe_costs, vibe_read_costs):
    assert selected_vibe_costs['subject_seed_commit'][0] == 1
    assert 'subject_seed_rollback' not in selected_vibe_costs
    with measured(vibe_read_costs, 'private_restore'):
        value = selected_vibe_template.restore(store)
    with measured(vibe_read_costs, 'private_restore'):
        second = selected_vibe_template.restore(file_backed_store)
    assert value == second == selected_vibe_template.result
    profiles = {}
    assert len(value['all']['fresh']) == 200
    assert set(value['_vibes']['pools']) == {'energize', 'relax'}
    for preset in ('energize', 'relax'):
        for lane in ('fresh', 'explore'):
            rows = value['_vibes']['pools'][preset][lane]
            assert len(rows) == 200
            profiles.update((item['key'], item['musical']) for item in rows)
            groups = value['_vibes']['genres'][preset][lane]
            assert set(groups) == {'techno', 'jazz'}
            for rows in groups.values():
                assert len(rows) == 12
                profiles.update((item['key'], item['musical']) for item in rows)
    assert len(profiles) == 896
    expected_subjects = set(profiles) | {item['key'] for item in value['all']['fresh']}
    assert len(expected_subjects) == 1096
    for target in (selected_vibe_template.source, store, file_backed_store):
        assert {row[0] for row in target.conn.execute('SELECT identity_key FROM recordings')} == expected_subjects
        actual = {}
        keys = list(profiles)
        for offset in range(0, len(keys), 512):
            actual.update(target.musical_profiles.by_keys(keys[offset:offset + 512]))
        assert actual == profiles  # Includes the full retained provenance strings.
        assert target.recommendations.heads() == selected_vibe_template.source.recommendations.heads()
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []

    # Prove the baseline's actual native file is copied, not borrowed or linked.
    publication = store.conn.execute('SELECT publication FROM recording_profile_files LIMIT 1').fetchone()[0]
    relative = profile_files.relative('recording', publication)
    paths = [profile_files.directory(target.conn) / relative
             for target in (store, file_backed_store, selected_vibe_template.source)]
    assert len(set(paths)) == 3
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
    original = paths[0].read_bytes()
    assert all(path.read_bytes() == original for path in paths)
    paths[0].write_bytes(b'Corrupted private fixture copy')
    assert paths[1].read_bytes() == paths[2].read_bytes() == original
    store.settings.set_setting('private_vibe_fixture', 'first consumer')
    for target in (file_backed_store, selected_vibe_template.source):
        assert target.settings.get_setting('private_vibe_fixture') is None
    value['_vibes']['pools']['energize']['fresh'][0]['musical']['dimensions']['intensity']['value'] = -1
    assert second == selected_vibe_template.result


def test_vibe_candidate_retry_is_detached_invalidated_and_rechecks_dislikes(store, monkeypatch, vibe_read_costs):
    # This case inserts discovery records before publication and later replaces
    # its head. Keep that producer sequence separate from the shared read input.
    value = bundles()
    items = value['_vibes']['pools']['energize']['fresh'][:12]
    with measured(vibe_read_costs, 'retry_existing_subject_seed'), store._lock, store.conn.atomic():
        for item in items:
            store.discovery.upsert_discovered_track(item['key'], item['video_id'], item['title'],
                item['artist'], '', None, item['genre'], None, None, 1)
    with measured(vibe_read_costs, 'retry_initial_publication'):
        publish(store, value, costs=vibe_read_costs)
    original_json = RecProposalsRepo._proposal_json
    def no_legacy_bundle(repo, surface, *args, **kwargs):
        if surface == 'mode_bundles':
            raise AssertionError('Selected typed reads must not inflate the retired global bundle')
        return original_json(repo, surface, *args, **kwargs)

    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', no_legacy_bundle)
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
