"""Compressed proposals retain logical pools, subset reads and policy ownership."""
import json

import pytest

from yt_playlist.core import json_cache, policy_config
from yt_playlist.rec import home_prepared, mode_surfaces, candidate_refresh
from yt_playlist.repos import policy_storage
from yt_playlist.repos.rec_proposals import encode_proposals


def _bundles(policy='prepared'):
    items = [{'key': f'song-{n}', 'video_id': f'video-{n}', 'title': f'Song {n}',
              'artist': 'Artist', 'description': 'repeated evidence ' * 60,
              'pool_policy': policy} for n in range(100)]
    return {'all': {'wheelhouse': items, 'fresh': items[:10]},
            '_meta': {'full_pools': True, 'comfort_pool': 0},
            '1': {'wheelhouse': items[:40]}, '_radio': {'1': [i['key'] for i in items]}}


@pytest.mark.parametrize('value', [None, {}, [], {'items': []}, 'plain', {'unicode': 'café 🎵'}])
def test_small_plain_json_is_unchanged(value):
    encoded = json_cache.encode(value)
    assert json.loads(encoded) == value
    assert json_cache.decode(encoded) == value


def test_compressed_bundle_and_fresh_pool_round_trip(store):
    bundles = _bundles()
    fresh = bundles['all']['wheelhouse']
    encoded, fresh_encoded = (encode_proposals('mode_bundles', bundles),
                              encode_proposals('fresh_pool', fresh))
    assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    assert len(encoded) < len(json.dumps(bundles)) // 4
    with store.conn:
        store.rec_proposals.publish_prepared_bundles(encoded, fresh_encoded, 10)
    assert store.rec_proposals.get_proposals('mode_bundles') == bundles
    assert store.rec_proposals.get_proposals('fresh_pool') == fresh
    assert json.loads(store.rec_proposals.group_pools_json()) == {
        'all': bundles['all'], '_meta': bundles['_meta']}
    assert encode_proposals('mode_bundles', bundles) == encoded


def test_group_subset_does_not_inflate_legacy_mode_copies(store, monkeypatch):
    bundles = _bundles()
    store.rec_proposals.put_proposals('mode_bundles', bundles, 10)
    envelope = json.loads(store.rec_proposals._proposal_json('mode_bundles'))
    original, unpacked = json_cache.unpack, []
    def unpack(encoded):
        unpacked.append(encoded)
        assert encoded != envelope['json'], 'Subset read inflated all legacy mode copies'
        return original(encoded)
    monkeypatch.setattr(json_cache, 'unpack', unpack)
    assert json.loads(store.rec_proposals.group_pools_json())['all'] == bundles['all']
    assert unpacked == [envelope['groups'], envelope['profiles']]


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
@pytest.mark.parametrize('keys', [None, set(), {'missing'}, {'1', '_radio'}, {'all', '_meta', '1'}])
def test_selected_cache_fields_preserve_complete_values_and_legacy_compatibility(format, keys):
    bundles = _bundles()
    profile = {'dimensions': {'intensity': {'value': .7, 'support': .5}}}
    for item in bundles['all']['wheelhouse']:
        item['musical'] = profile
    bundles['_vibes'] = {'home-only': bundles['all']['wheelhouse']}
    if format == 'plain':
        encoded = json.dumps(bundles)
    elif format == 'v1':
        encoded = json.dumps({json_cache.MARKER: json_cache.LEGACY_FORMAT,
                              'json': json_cache._pack(json.dumps(bundles))})
    else:
        encoded = encode_proposals('mode_bundles', bundles)
        assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    expected = bundles if keys is None else {key: value for key, value in bundles.items() if key in keys}
    decoded = json_cache.decode(encoded, keys=keys)
    assert decoded == expected
    if 'all' in decoded and '1' in decoded:
        decoded['all']['wheelhouse'][0]['musical']['dimensions']['intensity']['value'] = 0
        assert decoded['1']['wheelhouse'][0]['musical']['dimensions']['intensity']['value'] == .7
        assert profile['dimensions']['intensity']['value'] == .7


def test_subset_skips_unneeded_sections_and_profile_restoration(monkeypatch):
    bundles = _bundles()
    bundles['_vibes'] = {'must_not_restore': True, 'items': bundles['all']['wheelhouse']}
    encoded = encode_proposals('mode_bundles', bundles)
    envelope = json.loads(encoded)
    unpack, restore = json_cache.unpack, json_cache._restore_profiles

    def selected_unpack(value):
        assert value != envelope['groups'], 'Mode-only read inflated global pools'
        return unpack(value)

    def selected_restore(value, profiles):
        assert not isinstance(value, dict) or 'must_not_restore' not in value
        return restore(value, profiles)

    monkeypatch.setattr(json_cache, 'unpack', selected_unpack)
    monkeypatch.setattr(json_cache, '_restore_profiles', selected_restore)
    assert json_cache.decode(encoded, keys={'1', '_radio'}) == {
        '1': bundles['1'], '_radio': bundles['_radio']}


def test_old_compressed_cache_remains_readable(store):
    bundles = _bundles()
    pools = {'all': bundles['all'], '_meta': bundles['_meta']}
    old = {json_cache.MARKER: json_cache.LEGACY_FORMAT,
           'json': json_cache._pack(json.dumps(bundles)),
           'groups': json_cache._pack(json.dumps(pools))}
    with store.conn:
        store.rec_proposals.publish_prepared_bundles(json.dumps(old), '[]', 1)
    assert store.rec_proposals.get_proposals('mode_bundles') == bundles
    assert json.loads(store.rec_proposals.group_pools_json()) == pools


def test_shared_profiles_preserve_fields_values_order_and_independent_mutation(store):
    bundles = _bundles()
    # Distinctive evidence repeats far beyond zlib's window across logical pools.
    profiles = [{'dimensions': {f'dimension-{i}': (n * 113 + i * 37) / 997
                                for i in range(80)}, 'digest': f'evidence-{n}'}
                for n in range(100)]
    for n, item in enumerate(bundles['all']['wheelhouse']):
        item['musical'] = profiles[n]
    bundles['_vibes'] = {str(n): bundles['all']['wheelhouse'] for n in range(5)}
    original = json.dumps(bundles, separators=(',', ':'))
    legacy = json_cache._pack(original)
    encoded = encode_proposals('mode_bundles', bundles)
    envelope = json.loads(encoded)
    assert len(encoded) < len(legacy) // 2
    # The global pools occur only in the independently readable group section.
    assert 'all' not in json.loads(json_cache.unpack(envelope['json']))
    assert len(json.loads(json_cache.unpack(envelope['profiles']))) == 100
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1)
    decoded = store.rec_proposals.get_proposals('mode_bundles')
    assert decoded == bundles
    assert json.loads(store.rec_proposals.group_pools_json()) == {
        'all': bundles['all'], '_meta': bundles['_meta']}
    decoded['_vibes']['0'][0]['musical']['dimensions']['dimension-0'] = 'changed'
    assert decoded['_vibes']['1'][0]['musical'] == profiles[0]
    assert decoded['all']['wheelhouse'][0]['musical'] == profiles[0]
    assert json.dumps(bundles, separators=(',', ':')) == original


@pytest.mark.parametrize('profile', [None, 3, 'unknown', ['partial'], {'profile': 0},
                                   {'musical': {'profile': 'original'}, 'vector': [1, 2]}])
def test_profile_reference_shape_and_non_dictionary_values_round_trip(profile):
    value = {'items': [{'musical': profile, 'description': 'repeat ' * 15000}]}
    encoded = json_cache.encode(value)
    assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    assert json_cache.decode(encoded) == value


@pytest.mark.parametrize('value', [{}, {'all': {}}, {'_meta': {}}, {'other': []}])
def test_group_split_does_not_add_missing_keys(store, value):
    value = {**value, 'padding': 'repeat ' * 15000}
    store.rec_proposals.put_proposals('mode_bundles', value, 1)
    assert store.rec_proposals.get_proposals('mode_bundles') == value
    assert json.loads(store.rec_proposals.group_pools_json()) == {
        'all': value.get('all', {}), '_meta': value.get('_meta', {})}


@pytest.mark.parametrize('reference', [-1, 1, True, '0', None])
def test_corrupt_shared_profile_reference_fails_explicitly(reference):
    envelope = {json_cache.MARKER: json_cache.FORMAT,
                'json': json_cache._pack(json.dumps({'musical': {'profile': reference}})),
                'profiles': json_cache._pack(json.dumps(['{}']))}
    with pytest.raises(ValueError, match='compressed recommendation cache profile'):
        json_cache.decode(json.dumps(envelope))


def test_old_plain_json_cache_remains_readable(store):
    bundles = _bundles()
    fresh = bundles['all']['fresh']
    with store.conn:
        store.rec_proposals.publish_prepared_bundles(json.dumps(bundles), json.dumps(fresh), 1)
    assert store.rec_proposals.get_proposals('mode_bundles') == bundles
    assert store.rec_proposals.get_proposals('fresh_pool') == fresh
    assert json.loads(store.rec_proposals.group_pools_json()) == {
        'all': bundles['all'], '_meta': bundles['_meta']}


def test_decode_and_compression_run_outside_serving_lock(store, monkeypatch):
    unpack, pack = json_cache.unpack, json_cache._pack
    def outside_pack(encoded):
        assert not store._lock.held_by_current_thread
        return pack(encoded)
    def outside_unpack(encoded):
        assert not store._lock.held_by_current_thread
        return unpack(encoded)
    monkeypatch.setattr(json_cache, '_pack', outside_pack)
    monkeypatch.setattr(json_cache, 'unpack', outside_unpack)
    store.rec_proposals.put_proposals('mode_bundles', _bundles(), 1)
    assert store.rec_proposals.get_proposals('mode_bundles') == _bundles()
    assert json.loads(store.rec_proposals.group_pools_json())['all'] == _bundles()['all']


def test_compressed_proposals_protect_policy_references_during_retention(store):
    store.rec_ledger.record_serving_policy('prepared', {'ranker': 'retained'}, 1)
    store.rec_ledger.record_serving_policy('unused', {'ranker': 'retired'}, 1)
    store.rec_proposals.put_proposals('mode_bundles', _bundles(), 1)
    assert 'prepared' in policy_storage.protected_references(store.conn)
    store.rec_ledger._prune_policy_batch(10000)
    assert store.rec_ledger.policy_configuration('prepared') == {'ranker': 'retained'}
    assert store.rec_ledger.policy_configuration('unused') is None


@pytest.mark.parametrize('outcome', ['success', 'failure', 'stale'])
def test_encoded_bundles_policies_and_settings_publish_atomically(store, outcome):
    store.rec_proposals.put_proposals('mode_bundles', {'old': True}, 1)
    store.rec_proposals.put_proposals('fresh_pool', ['old'], 1)
    store.settings.set_setting('input', 'old')
    row, base = policy_config.pack({'ranker': 'new'}, 10)
    bundles = _bundles(row[0])
    encoded = encode_proposals('mode_bundles', bundles)
    fresh = encode_proposals('fresh_pool', bundles['all']['fresh'])
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('input') == 'old'
        if outcome == 'stale':
            store.settings.set_setting('input', 'changed')
        def publish():
            store.rec_ledger.publish_prepared_policies([row], [base])
            store.rec_proposals.publish_prepared_bundles(encoded, fresh, 10)
            store.settings.publish_values({'published-input': 'new'})
            if outcome == 'failure':
                raise RuntimeError('injected publication failure')
        if outcome == 'failure':
            with pytest.raises(RuntimeError, match='injected publication'):
                snapshot.publish(publish)
        else:
            assert snapshot.publish(publish) is (outcome == 'success')
    assert not store.conn.in_transaction
    if outcome == 'success':
        assert store.rec_proposals.get_proposals('mode_bundles') == bundles
        assert store.rec_proposals.get_proposals('fresh_pool') == bundles['all']['fresh']
        assert store.settings.get_setting('published-input') == 'new'
        assert store.rec_ledger.policy_configuration(row[0]) == {'ranker': 'new'}
    else:
        assert store.rec_proposals.get_proposals('mode_bundles') == {'old': True}
        assert store.rec_proposals.get_proposals('fresh_pool') == ['old']
        assert store.settings.get_setting('published-input') is None
        assert store.rec_ledger.policy_configuration(row[0]) is None


def test_bundle_preparation_encodes_before_guarded_publication(store, monkeypatch):
    bundles = _bundles()
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles',
                        lambda *args: (bundles, bundles['all']['fresh']))
    monkeypatch.setattr(candidate_refresh, 'prepared_settings', lambda *args: {'prepared': 'yes'})
    original = json_cache._pack
    def pack(encoded):
        assert not store._lock.held_by_current_thread
        assert not store.conn.in_transaction
        return original(encoded)
    monkeypatch.setattr(json_cache, '_pack', pack)
    assert mode_surfaces.prepare_bundles(store, 10) == bundles
    assert store.rec_proposals.get_proposals('mode_bundles') == bundles
    assert store.settings.get_setting('prepared') == 'yes'


def test_fingerprint_is_stable_across_subset_reads_and_changes_with_new_content(store):
    bundles = _bundles()
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1)
    store.rec_proposals.put_proposals('fresh_pool', bundles['all']['fresh'], 1)
    def generation():
        return home_prepared._proposal_generation(store, store.rec_proposals.group_pools_json(), 10)
    before = generation()
    assert generation() == before
    bundles['all']['wheelhouse'][0]['title'] = 'Changed'
    store.rec_proposals.put_proposals('mode_bundles', bundles, 2)
    assert generation() != before


def test_discovery_cleanup_prunes_compressed_cached_tracks(store):
    bundles = _bundles()
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1)
    store.rec_proposals.put_proposals('fresh_pool', bundles['all']['wheelhouse'], 1)
    assert store.discovery._prune_cached_tracks({'song-1'})
    cleaned = store.rec_proposals.get_proposals('mode_bundles')
    assert 'song-1' not in {item['key'] for item in cleaned['all']['wheelhouse']}
    assert 'song-1' not in {item['key'] for item in cleaned['1']['wheelhouse']}
    assert len(store.rec_proposals.get_proposals('fresh_pool')) == 99
    assert json.loads(store.rec_proposals.group_pools_json())['all'] == cleaned['all']
    assert json.loads(store.rec_proposals._proposal_json('mode_bundles'))[json_cache.MARKER] == json_cache.FORMAT


@pytest.mark.parametrize('body', [None, 'invalid base64!', 'eA=='])
def test_invalid_compressed_cache_reports_value_error(body):
    with pytest.raises(ValueError, match='compressed recommendation cache'):
        json_cache.decode(json.dumps({json_cache.MARKER: json_cache.FORMAT, 'json': body}))
