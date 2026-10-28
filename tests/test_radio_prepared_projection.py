"""Radio selects the same live lookahead before expanding musical evidence."""
from copy import deepcopy
from itertools import zip_longest
import json

import pytest

from tests.test_radio_catalog import seed  # noqa: F401 - real prepared-mode fixture
from yt_playlist.core import json_cache
from yt_playlist.rec import musical_character, musical_profiles, radio_candidates, radio_tuning
from yt_playlist.repos.rec_proposals import encode_proposals


def _bundles():
    tracks = [{'key': f'song-{i}', 'video_id': f'video-{i}' if i % 19 else None,
               'artist': 'Muted' if i % 17 == 0 else f'Artist {i}',
               'description': 'retained metadata ' * 30,
               'musical': {'version': 'fixture', 'dimensions': {'energy': {'value': i / 200}},
                           'provenance': ['measured', {'recording': i}]}}
              for i in range(200)]
    bucket = {'wheelhouse': tracks[:110], 'explore': tracks[20:130],
              'comfort': tracks[::2], 'temporal': tracks[::-3], 'fresh': tracks[100:]}
    return {'1': bucket, '2': bucket, 'all': bucket,
            '_meta': {'full_pools': True, 'musical_generation': 'musical'},
            '_ppr': {'1': [track['key'] for track in tracks]},
            '_radio': {'generation': 'basis', 'space': 'space',
                       'members': {'1': [track['key'] for track in tracks]}}}


def _original_pool(bucket, mid, blocked, muted, comfort):
    """Original constructor calculation, independent of the new projection."""
    names = ('wheelhouse', 'explore', 'comfort', 'temporal', 'fresh')
    rows = [[{**t, '_radio_fresh': name == 'fresh', '_radio_comfort': name == 'comfort'}
             for t in bucket.get(name, [])] for name in names]
    pool, seen = [], set()
    for group in zip_longest(*rows):
        for item in group:
            if not item or not item.get('video_id') or item['key'] in seen | blocked:
                continue
            if (item.get('artist') or '').casefold() in muted:
                continue
            track = {**item, '_radio_mode': mid}
            seen.add(track['key'])
            pool.append(track)
    fresh = [t for t in pool if t.get('_radio_fresh')]
    familiar = [t for t in pool if not t.get('_radio_fresh')]
    quota = round(radio_candidates.CLUSTER_POOL_LIMIT * .85 * (1 - comfort))
    reserve = fresh[:quota] + familiar[:radio_candidates.CLUSTER_POOL_LIMIT - min(quota, len(fresh))]
    used = {t['key'] for t in reserve}
    return (reserve + [t for t in pool if t['key'] not in used])[:radio_candidates.CLUSTER_POOL_LIMIT]


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
@pytest.mark.parametrize('comfort', [0, .5, 1])
def test_projected_lookahead_matches_original_order_quota_and_profiles(monkeypatch, format, comfort):
    bundles = _bundles()
    original = deepcopy(bundles)
    if format == 'plain':
        encoded = json.dumps(bundles)
    elif format == 'v1':
        encoded = json.dumps({json_cache.MARKER: json_cache.LEGACY_FORMAT,
                              'json': json_cache._pack(json.dumps(bundles))})
    else:
        encoded = encode_proposals('mode_bundles', bundles)
        assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    blocked, muted = {'song-1', 'song-7', 'song-102'}, {'muted'}
    expected = {mid: _original_pool(bundles[mid], mid, blocked, muted, comfort) for mid in ('1', '2')}
    restore, restored = json_cache._restore_profiles, []

    def observed(value, profiles):
        if isinstance(value, dict) and 'musical' in value:
            restored.append(value['key'])
        return restore(value, profiles)

    monkeypatch.setattr(json_cache, '_restore_profiles', observed)
    decoded = json_cache.decode(encoded, project=lambda value: radio_candidates._initial_projection(
        value, {'1': {}, '2': {}}, 'space', 'basis', blocked, muted, comfort))
    assert {mid: decoded[mid]['_radio_pool'] for mid in ('1', '2')} == expected
    assert 'all' not in decoded
    for key in ('_meta', '_ppr', '_radio'):
        assert decoded[key] == original[key]
    if format == 'v2':
        assert len(restored) == sum(map(len, expected.values()))
    decoded['1']['_radio_pool'][0]['musical']['provenance'][1]['recording'] = 'changed'
    assert decoded['2']['_radio_pool'] == expected['2']
    assert bundles == original
    # A projection never truncates or rewrites the stored complete collections.
    assert json_cache.decode(encoded) == original


@pytest.mark.parametrize('catalog', ['matching', 'stale', 'wrong-space', 'missing'])
def test_empty_mode_keeps_indexed_refill_or_the_complete_cold_fallback(catalog):
    bundles = _bundles()
    bundles['1'] = {}
    if catalog == 'stale':
        bundles['_radio']['generation'] = 'old'
    elif catalog == 'wrong-space':
        bundles['_radio']['space'] = 'old'
    elif catalog == 'missing':
        bundles.pop('_radio')
    decoded = json_cache.decode(encode_proposals('mode_bundles', bundles),
        project=lambda value: radio_candidates._initial_projection(
            value, {'1': {}}, 'space', 'basis', set(), set(), .5))
    assert decoded['1']['_radio_pool'] == []
    if catalog == 'matching':
        assert 'all' not in decoded
        assert decoded['_radio']['members'] == bundles['_radio']['members']
    else:
        assert decoded['all'] == bundles['all']


@pytest.mark.parametrize('comfort', [0, .5, 1])
def test_real_constructor_preserves_full_session_state_and_stored_pools(store, seed, monkeypatch, comfort):
    bundles = _bundles()
    profile = musical_character.compact(musical_profiles.resolve([], overrides={'energy': .7}))
    for track in bundles['all']['wheelhouse']:
        track['musical'] = profile
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1000)
    tuning = {**radio_tuning.DEFAULTS, 'comfort': comfort}
    projected = radio_candidates.RadioCandidates(store, 1000, tuning, comparison=False)
    monkeypatch.setattr(radio_candidates, '_initial_projection', lambda value, *args: value)
    original = radio_candidates.RadioCandidates(store, 1000, tuning, comparison=False)
    for field in ('pools', 'prior', 'by_key', 'by_video', 'membership', 'energies',
                  'fingerprint', 'cluster_members', 'now_posterior', 'now_gain'):
        assert getattr(projected, field) == getattr(original, field), field
    assert projected.pools['1']
    assert store.rec_proposals.get_proposals('mode_bundles') == bundles


@pytest.mark.parametrize('value', [None, {}, []])
def test_missing_prepared_values_still_reach_the_real_catalog_fallback(store, seed, value):
    store.rec_proposals.put_proposals('mode_bundles', value, 1000)
    candidates = radio_candidates.RadioCandidates(store, 1000, comparison=False)
    assert candidates.pools['local']
    assert seed['video_id'] in candidates.by_video


def test_input_reuse_reprojects_current_exclusions_and_refreshes_on_publication(store, seed):
    bundles = _bundles()
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1000)

    def inputs(blocked):
        return radio_candidates._prepared_inputs(store,
            project=lambda value, modes, space, generation: radio_candidates._initial_projection(
                value, {'1': {}}, 'space', 'basis', blocked, set(), .5))[-1]

    first = inputs(set())
    key = first['1']['_radio_pool'][0]['key']
    second = inputs({key})
    assert key not in {track['key'] for track in second['1']['_radio_pool']}
    first['1']['_radio_pool'][0]['musical']['provenance'][1]['recording'] = 'changed'
    assert inputs(set())['1']['_radio_pool'][0]['musical']['provenance'][1]['recording'] != 'changed'
    bundles['_meta']['musical_generation'] = 'replacement'
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1001)
    assert inputs(set())['_meta']['musical_generation'] == 'replacement'


def test_owned_transaction_does_not_wait_for_a_shared_input_decode(store, seed, monkeypatch):
    def forbidden(_store):
        pytest.fail('A serving-lock owner must not wait for a shared decoder')

    monkeypatch.setattr(radio_candidates, '_input_cache', forbidden)
    with store._lock:
        assert radio_candidates._prepared_inputs(store)[-1]
