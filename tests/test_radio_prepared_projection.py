"""Radio selects the same live lookahead before expanding musical evidence."""
from copy import deepcopy
from dataclasses import replace
from itertools import zip_longest
import json

import pytest

from tests.conftest import seed_catalog_track
from tests.group_support import publish, recordings
from tests.recommendation_support import publish_bundles
from tests.test_radio_catalog import catalog_track, seed  # noqa: F401 - real prepared-mode fixture
from yt_playlist.core import json_cache
from yt_playlist.rec import (embedding_vectors, musical_character, musical_profiles, radio_candidate_inputs,
                             radio_candidates, radio_tuning)
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.core.proposal_codec import encode_proposals


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
    decoded = json_cache.decode(encoded, project=lambda value: radio_candidate_inputs.initial_projection(
        value, {'1': {}, '2': {}}, 'space', 'basis', blocked, muted, comfort))
    assert {mid: decoded[mid]['_radio_pool'] for mid in ('1', '2')} == expected
    fresh = [track for track in original['all']['fresh'] if track['video_id']
             and track['key'] not in blocked and track['artist'].casefold() not in muted]
    assert decoded['all'] == {'fresh': fresh[:radio_candidates.CLUSTER_POOL_LIMIT]}
    for key in ('_meta', '_ppr', '_radio'):
        assert decoded[key] == original[key]
    if format == 'v2':
        assert len(restored) == sum(map(len, expected.values())) + len(decoded['all']['fresh'])
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
        project=lambda value: radio_candidate_inputs.initial_projection(
            value, {'1': {}}, 'space', 'basis', set(), set(), .5))
    assert decoded['1']['_radio_pool'] == []
    if catalog == 'matching':
        assert decoded['all'] == {'fresh': [track for track in bundles['all']['fresh'] if track['video_id']][
            :radio_candidates.CLUSTER_POOL_LIMIT]}
        assert decoded['_radio']['members'] == bundles['_radio']['members']
    else:
        assert decoded['all'] == bundles['all']


@pytest.mark.parametrize('comfort,spaces', [(0, 'match'), (.5, 'match'), (1, 'match'),
                                           (.5, 'retained'), (.5, 'changed'), (.5, 'home-only')])
def test_typed_reserves_load_on_demand_in_original_order(store, monkeypatch, comfort, spaces):
    bundles = _bundles()
    tracks = {track['key']: track for bucket in bundles.values() if isinstance(bucket, dict)
              for items in bucket.values() if isinstance(items, list)
              for track in items if isinstance(track, dict)}
    corpus = recordings([('Downtempo', 100, .1), ('Jungle', 100, .9)])
    for track, recording in zip(tracks.values(), corpus):
        track['title'] = track['key']
        track['genre'] = recording.genre
        track.pop('description')
        seed_catalog_track(store, track['key'], track['title'], track['artist'], video_id=track['video_id'],
                           genre=track['genre'])
    head = publish(store, [replace(recording, key=key) for key, recording in zip(tracks, corpus)])
    membership = store.taste_groups.leaf_members(head['generation'])
    modes = sorted(set(membership.values()))
    assert len(modes) == 2
    del bundles['1'], bundles['2']
    for mode in modes:
        bundles[mode] = {lane: [track for track in items if membership[track['key']] == mode]
                         for lane, items in bundles['all'].items()}
    bundles['_ppr'] = {mode: [key for key in tracks if membership[key] == mode] for mode in modes}
    bundles['_radio']['members'] = {} if spaces == 'home-only' else bundles['_ppr']
    profile = musical_character.compact(musical_profiles.resolve([], overrides={'intensity': .7}))
    for track in bundles['all']['wheelhouse']:
        track['musical'] = profile
    published_space = embedding_vectors.content_space_id(store) if spaces == 'match' else 'published-space'
    bundles['_radio'].update(space=published_space,
                              generation=store.rec_model.recording_state().generation)
    publish_bundles(store, bundles, 1000)
    if spaces == 'changed':
        monkeypatch.setattr(embedding_vectors, 'content_space_id', lambda *args, **kwargs: 'different-space')
    head = store.recommendations.head('home')
    assert json.loads(head['evidence_roots']) == []
    ranks = list(map(tuple, store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY pool_id,rank')))
    tuning = {**radio_tuning.DEFAULTS, 'comfort': comfort}
    batches, candidate_rows = [], RecommendationPublicationsRepo.candidate_rows

    def observed_pages(self, surface, selections, **kwargs):
        batches.append(list(selections))
        return candidate_rows(self, surface, selections, **kwargs)

    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_rows', observed_pages)
    capture = PreparedRadio.capture_inputs

    def unprojected(self, reader, modes, space, generation, blocked, muted, comfort, *, adaptive=False):
        if spaces in ('retained', 'changed'):
            space = published_space
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

    projected = radio_candidates.RadioCandidates(store, 1000, tuning)
    original = None
    try:
        assert not batches and not any(projected.pools.values())
        assert set(projected.cluster_members) == (set() if spaces == 'home-only' else set(modes))
        projected._initial_track_group_pool(modes[0])
        assert batches and all(len(batch) <= 16 for batch in batches)
        assert len(batches) < sum(len(batch) for batch in batches), 'Initial lanes must share catalog hydration'
        assert {selection[0] for batch in batches for selection in batch} == {modes[0]}
        assert not projected.pools[modes[1]]
        projected._initial_track_group_pool(modes[1])
        monkeypatch.setattr(PreparedRadio, 'capture_inputs', unprojected)
        original = radio_candidates.RadioCandidates(store, 1000, tuning)
        for field in ('pools', 'prior', 'cluster_members'):
            assert getattr(projected, field) == getattr(original, field), field
        assert set(projected.pools) == set(modes) and all(projected.pools.values())
        if spaces == 'home-only':
            seed = projected.pools[modes[0]][0]
            with projected.prepared.planning_read():
                assert projected.next_track([seed])
                assert projected.direction_anchor([seed], [])
        open_head = store.recommendations.head('home')
        leases = json.loads(open_head['evidence_roots'])
        assert len(leases) == 2 and leases[0] != leases[1]
        assert all(kind == 'pending' and key for kind, key in leases)
        assert {**open_head, 'evidence_roots': head['evidence_roots']} == head
        assert list(map(tuple, store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY pool_id,rank'))) == ranks
    finally:
        projected.close()
        if original is not None:
            original.close()
    assert store.recommendations.head('home') == head


@pytest.mark.parametrize('value', [None, {}, []])
def test_missing_prepared_values_still_reach_the_real_catalog_fallback(store, value):
    assert json_cache.decode(encode_proposals('mode_bundles', value)) == value
    seed, entry = catalog_track(store, 'seed')
    account = store.accounts.upsert_account('Main', None, True)
    playlist = store.playlists.upsert_playlist(account, 'LIBRARY', 'Library', 1, 'hash', 1)
    store.playlists.set_playlist_tracks(playlist, [entry])
    assert store.recommendations.head('home') is None
    candidates = radio_candidates.RadioCandidates(store, 1000)
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
        return cache.decode(encoded, project=lambda value: radio_candidate_inputs.initial_projection(
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


def test_owned_transaction_uses_the_already_locked_writer(store, seed, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail('A serving-lock owner must use its own connection')

    monkeypatch.setattr(store, 'read_view', forbidden)
    prepared = PreparedRadio(store, 1000)
    try:
        with store._lock:
            assert radio_candidate_inputs.prepared_inputs(store, prepared=prepared)[-1]
    finally:
        prepared.close()
