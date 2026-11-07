"""Radio selects the same live lookahead before expanding musical evidence."""
from copy import deepcopy
from itertools import zip_longest
import json

import pytest

from tests.conftest import seed_catalog_track
from tests.recommendation_support import publish_bundles
from tests.test_radio_catalog import catalog_track, seed  # noqa: F401 - real prepared-mode fixture
from yt_playlist.core import json_cache
from yt_playlist.rec import embedding_vectors, musical_character, musical_profiles, radio_candidates, radio_tuning
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
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
    tracks = {track['key']: track for bucket in bundles.values() if isinstance(bucket, dict)
              for items in bucket.values() if isinstance(items, list)
              for track in items if isinstance(track, dict)}
    for track in tracks.values():
        track['title'] = track['key']
        track.pop('description')
        seed_catalog_track(store, track['key'], track['title'], track['artist'], video_id=track['video_id'])
    profile = musical_character.compact(musical_profiles.resolve([], overrides={'energy': .7}))
    for track in bundles['all']['wheelhouse']:
        track['musical'] = profile
    bundles['_radio'].update(space=embedding_vectors.content_space_id(store),
                              generation=store.settings.get_setting('rec_content_generation'))
    publish_bundles(store, bundles, 1000)
    head = store.recommendations.head('home')
    ranks = list(map(tuple, store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY pool_id,rank')))
    tuning = {**radio_tuning.DEFAULTS, 'comfort': comfort}
    batches, candidate_rows = [], RecommendationPublicationsRepo.candidate_rows

    def observed_pages(self, surface, selections, **kwargs):
        batches.append(list(selections))
        return candidate_rows(self, surface, selections, **kwargs)

    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_rows', observed_pages)
    projected = radio_candidates.RadioCandidates(store, 1000, tuning, comparison=False)
    assert batches and all(len(batch) <= 16 for batch in batches)
    assert len(batches) < sum(len(batch) for batch in batches), 'Initial lanes must share catalog hydration'
    capture = PreparedRadio.capture_inputs

    def unprojected(self, reader, modes, space, generation, blocked, muted, comfort):
        result, _displays, _pools = capture(self, reader, modes, space, generation, blocked, muted, comfort)
        for mode in modes:
            bucket = {}
            for lane in ('wheelhouse', 'explore', 'comfort', 'temporal', 'fresh'):
                rows, after = [], -1
                while True:
                    page = reader.recommendations.candidate_page('home', mode, lane,
                        generation=self.generation, after=after, limit=128)
                    rows.extend(page['tracks'])
                    after = page['after']
                    if page['exhausted']:
                        break
                bucket[lane] = rows
            result[mode] = bucket
        # The reference constructor receives complete hydrated lanes from the pinned
        # reader, rather than the bounded pools selected by the production capture.
        return result, [], []

    monkeypatch.setattr(PreparedRadio, 'capture_inputs', unprojected)
    original = None
    try:
        original = radio_candidates.RadioCandidates(store, 1000, tuning, comparison=False)
        for field in ('pools', 'prior', 'by_key', 'by_video', 'membership', 'energies',
                      'fingerprint', 'cluster_members', 'now_posterior', 'now_gain'):
            assert getattr(projected, field) == getattr(original, field), field
        assert projected.pools['1']
        assert store.recommendations.head('home') == head
        assert list(map(tuple, store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY pool_id,rank'))) == ranks
    finally:
        projected.close()
        if original is not None:
            original.close()


@pytest.mark.parametrize('value', [None, {}, []])
def test_missing_prepared_values_still_reach_the_real_catalog_fallback(store, value):
    assert json_cache.decode(encode_proposals('mode_bundles', value)) == value
    seed, entry = catalog_track(store, 'seed')
    account = store.accounts.upsert_account('Main', 'credential', None, True)
    playlist = store.playlists.upsert_playlist(account, 'LIBRARY', 'Library', 1, 'hash', 1)
    store.playlists.set_playlist_tracks(playlist, [entry])
    assert store.recommendations.head('home') is None
    candidates = radio_candidates.RadioCandidates(store, 1000, comparison=False)
    try:
        assert candidates.pools['local']
        assert seed['video_id'] in candidates.by_video
    finally:
        candidates.close()


def test_input_reuse_reprojects_current_exclusions_and_refreshes_on_publication():
    bundles = _bundles()
    encoded = encode_proposals('mode_bundles', bundles)
    cache = json_cache.DecodeCache()

    def inputs(blocked):
        return cache.decode(encoded, project=lambda value: radio_candidates._initial_projection(
            value, {'1': {}}, 'space', 'basis', blocked, set(), .5))

    first = inputs(set())
    key = first['1']['_radio_pool'][0]['key']
    second = inputs({key})
    assert key not in {track['key'] for track in second['1']['_radio_pool']}
    first['1']['_radio_pool'][0]['musical']['provenance'][1]['recording'] = 'changed'
    assert inputs(set())['1']['_radio_pool'][0]['musical']['provenance'][1]['recording'] != 'changed'
    bundles['_meta']['musical_generation'] = 'replacement'
    encoded = encode_proposals('mode_bundles', bundles)
    assert inputs(set())['_meta']['musical_generation'] == 'replacement'


def test_owned_transaction_does_not_wait_for_a_shared_input_decode(store, seed, monkeypatch):
    def forbidden(_store):
        pytest.fail('A serving-lock owner must not wait for a shared decoder')

    monkeypatch.setattr(radio_candidates, '_input_cache', forbidden)
    prepared = PreparedRadio(store, 1000)
    try:
        with store._lock:
            assert radio_candidates._prepared_inputs(store, prepared=prepared)[-1]
    finally:
        prepared.close()
