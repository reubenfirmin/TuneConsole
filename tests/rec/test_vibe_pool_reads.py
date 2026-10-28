"""Selected prepared reads retain candidate order and current eligibility."""
from copy import deepcopy
import json

import pytest

from yt_playlist.core import json_cache
from yt_playlist.rec import home_vibe, vibe_pools
from yt_playlist.repos.rec_proposals import RecProposalsRepo, encode_proposals


def bundles():
    def items(preset, lane, family='general', count=200):
        return [{'key': f'{preset}:{lane}:{family}:{i}', 'video_id': f'v-{preset}-{lane}-{family}-{i}',
                 'title': f'Song {i}', 'artist': f'Artist {i}', 'genre': 'Techno' if i % 2 else 'Jazz',
                 'score': i / 100, 'pool_source': f'{preset}:{lane}', 'branch': family,
                 'musical': {'dimensions': {'intensity': {'value': i / count, 'support': .8}},
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


def publish(store, value, format='v2'):
    if format == 'plain':
        encoded = json.dumps(value)
    elif format == 'v1':
        encoded = json.dumps({json_cache.MARKER: json_cache.LEGACY_FORMAT,
                              'json': json_cache._pack(json.dumps(value))})
    else:
        encoded = encode_proposals('mode_bundles', value)
        assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,1) '
                           'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload',
                           ('mode_bundles', encoded))


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
@pytest.mark.parametrize('lane', ['fresh', 'explore'])
@pytest.mark.parametrize('include,avoid', [((), ()), (('techno',), ()), ((), ('jazz',)),
                                          (('jazz', 'techno'), ('techno',))])
def test_selected_vibe_read_matches_full_decode_and_skips_unrelated_profiles(
        store, monkeypatch, format, lane, include, avoid):
    value = bundles()
    publish(store, value, format)
    original = store.rec_proposals._proposal_json('mode_bundles')
    expected = vibe_pools.candidates(store, 'energize', lane, bundles=deepcopy(value),
                                     include_genres=include, avoid_genres=avoid)
    restore = json_cache._restore_profiles
    visited = []

    def selected(item, profiles):
        if isinstance(item, dict) and 'pool_source' in item:
            visited.append(item['pool_source'])
            assert item['pool_source'] == f'energize:{lane}'
            family = item['branch']
            if family != 'general':
                assert (include or avoid) and family not in avoid and (not include or family in include)
        return restore(item, profiles)

    monkeypatch.setattr(json_cache, '_restore_profiles', selected)
    assert vibe_pools.candidates(store, 'energize', lane,
        include_genres=include, avoid_genres=avoid) == expected
    assert len(expected) <= vibe_pools.POOL_LIMIT
    if format == 'v2':
        assert visited
    assert store.rec_proposals._proposal_json('mode_bundles') == original


def test_vibe_candidate_retry_is_detached_invalidated_and_rechecks_dislikes(store, monkeypatch):
    value = bundles()
    items = value['_vibes']['pools']['energize']['fresh'][:12]
    for item in items:
        store.discovery.upsert_discovered_track(item['key'], item['video_id'], item['title'],
            item['artist'], '', None, item['genre'], None, None, 1)
    publish(store, value)
    read = RecProposalsRepo._proposal_json
    calls = []

    def counted(repo, surface):
        if surface == 'mode_bundles':
            calls.append(surface)
        return read(repo, surface)

    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', counted)
    with store.read_snapshot() as snapshot:
        first = vibe_pools.candidates(snapshot.store, 'energize', 'fresh')
        first[0]['musical']['dimensions']['intensity']['value'] = -1
        selected = vibe_pools.pick(snapshot.store, 1000, 'energize', 'fresh')
    assert selected and len(calls) == 1
    key = selected[0]['key']
    store.rec_model.record_dislike(key, until=None, now=1001)
    with store.read_snapshot() as snapshot:
        second = vibe_pools.candidates(snapshot.store, 'energize', 'fresh')
        assert second[0]['musical']['dimensions']['intensity']['value'] >= 0
        assert key not in {item['key'] for item in vibe_pools.pick(snapshot.store, 1002, 'energize', 'fresh')}
    assert len(calls) == 1
    replacement = deepcopy(value)
    replacement['_vibes']['pools']['energize']['fresh'][0]['title'] = 'New completed pool'
    publish(store, replacement)
    with store.read_snapshot() as snapshot:
        assert vibe_pools.candidates(snapshot.store, 'energize', 'fresh')[0]['title'] == 'New completed pool'
    assert len(calls) == 2


@pytest.mark.parametrize('value', [None, {}, {'_vibes': {'version': 'older-preset-policy'}}])
def test_missing_or_older_vibe_candidates_remain_empty(store, value):
    publish(store, value, 'plain')
    assert vibe_pools.candidates(store, 'energize', 'fresh') == []
