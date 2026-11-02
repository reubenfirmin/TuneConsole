"""Selected prepared reads retain candidate order and current eligibility."""
from copy import deepcopy
import json

import pytest

from yt_playlist.core import json_cache
from tests.recommendation_support import publish_bundles, seed_bundle_subjects
from yt_playlist.rec import home_vibe, musical_character, musical_profiles, vibe_pools
from yt_playlist.repos.rec_proposals import RecProposalsRepo, encode_proposals


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


def publish(store, value):
    # Current reads consume the same typed publication for every private input
    # encoding. Exercise codec parity above without repeating database setup.
    value = value or {}
    prepared = value.get('_vibes') or {}
    items = [item for lanes in prepared.get('pools', {}).values() for rows in lanes.values() for item in rows]
    items += [item for lanes in prepared.get('genres', {}).values() for groups in lanes.values()
              for rows in groups.values() for item in rows]
    seed_bundle_subjects(store, value, fresh=items)
    profiles = {item['key']: item['musical'] for item in items}
    with store._lock, store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.musical_profiles.publish_recordings([(key, musical_profiles.canonical(profile), profile['version'],
            musical_profiles.digest(profile)) for key, profile in profiles.items()], 1)
    publish_bundles(store, value, 1)


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


@pytest.mark.parametrize('lane', ['fresh', 'explore'])
def test_selected_vibe_read_matches_full_decode_and_skips_unrelated_profiles(
        store, monkeypatch, lane):
    value = bundles()
    publish(store, value)
    decoded = full_decode(store)
    load = musical_character.load
    # Each scenario reads the same immutable publication. Keep both lanes and
    # every filter combination, with a fresh spy for each bounded read.
    for include, avoid in [((), ()), (('techno',), ()), ((), ('jazz',)),
                           (('jazz', 'techno'), ('techno',))]:
        expected = vibe_pools.candidates(store, 'energize', lane, bundles=decoded,
                                        include_genres=include, avoid_genres=avoid)
        visited = []

        def selected(reader, keys):
            visited.extend(keys)
            assert len(keys) <= vibe_pools.POOL_LIMIT
            for key in keys:
                preset, selected_lane, family, _ = key.split(':')
                assert preset == 'energize' and selected_lane == lane
                if family != 'general':
                    assert (include or avoid) and family not in avoid and (not include or family in include)
            return load(reader, keys)

        with monkeypatch.context() as scenario:
            scenario.setattr(musical_character, 'load', selected)
            assert vibe_pools.candidates(store, 'energize', lane,
                include_genres=include, avoid_genres=avoid) == expected, (lane, include, avoid)
        assert len(expected) <= vibe_pools.POOL_LIMIT
        assert visited, (lane, include, avoid)


def test_vibe_candidate_retry_is_detached_invalidated_and_rechecks_dislikes(store, monkeypatch):
    value = bundles()
    items = value['_vibes']['pools']['energize']['fresh'][:12]
    for item in items:
        store.discovery.upsert_discovered_track(item['key'], item['video_id'], item['title'],
            item['artist'], '', None, item['genre'], None, None, 1)
    publish(store, value)
    original_json = RecProposalsRepo._proposal_json
    def no_legacy_bundle(repo, surface, *args, **kwargs):
        if surface == 'mode_bundles':
            raise AssertionError('Selected typed reads must not inflate the retired global bundle')
        return original_json(repo, surface, *args, **kwargs)

    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', no_legacy_bundle)
    with store.read_snapshot() as snapshot:
        first = vibe_pools.candidates(snapshot.store, 'energize', 'fresh')
        first[0]['musical']['dimensions']['intensity']['value'] = -1
        selected = vibe_pools.pick(snapshot.store, 1000, 'energize', 'fresh')
    assert selected
    key = selected[0]['key']
    store.rec_model.record_dislike(key, until=None, now=1001)
    with store.read_snapshot() as snapshot:
        second = vibe_pools.candidates(snapshot.store, 'energize', 'fresh')
        assert second[0]['musical']['dimensions']['intensity']['value'] >= 0
        assert key not in {item['key'] for item in vibe_pools.pick(snapshot.store, 1002, 'energize', 'fresh')}
    replacement = deepcopy(value)
    replacement['_vibes']['pools']['energize']['fresh'].reverse()
    expected = replacement['_vibes']['pools']['energize']['fresh'][0]['key']
    previous_head = store.recommendations.head('vibe:energize')['id']
    publish(store, replacement)
    assert store.recommendations.head('vibe:energize')['id'] != previous_head
    with store.read_snapshot() as snapshot:
        assert vibe_pools.candidates(snapshot.store, 'energize', 'fresh')[0]['key'] == expected


@pytest.mark.parametrize('value', [None, {}, {'_vibes': {'version': 'older-preset-policy'}}])
def test_missing_or_older_vibe_candidates_remain_empty(store, value):
    publish(store, value)
    assert vibe_pools.candidates(store, 'energize', 'fresh') == []
