"""Shared parsing must preserve exact payload identity and independent consumers."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from yt_playlist.core import json_cache
from yt_playlist.core.activity import cancellable_work, WorkClosing


def _encoded(format, value):
    if format == 'plain':
        return json.dumps(value)
    if format == 'v1':
        return json.dumps({json_cache.MARKER: json_cache.LEGACY_FORMAT,
                           'json': json_cache._pack(json.dumps(value))})
    encoded = json_cache.encode(value, group_pools=True)
    assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    return encoded


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
def test_shared_parse_applies_each_projection_and_detaches_results(format, monkeypatch):
    tracks = [{'key': str(i), 'description': 'evidence ' * 1500,
               'musical': {'provenance': [{'recording': i}]}} for i in range(8)]
    value = {'all': {'wheelhouse': tracks}, '1': {'wheelhouse': tracks},
             '_meta': {'complete': True}, 'excluded': {'must_not_return': True}}
    encoded = _encoded(format, value)
    read, calls = json_cache._read, []

    def observed(*args):
        calls.append(True)
        return read(*args)

    monkeypatch.setattr(json_cache, '_read', observed)
    cache = json_cache.DecodeCache()
    keys = {'all', '1', '_meta'}
    first = cache.decode(encoded, keys=keys,
                         project=lambda body: {'picked': body['1']['wheelhouse'][:2]})
    first['picked'][0]['musical']['provenance'][0]['recording'] = 'changed'
    second = cache.decode(encoded, keys=keys,
                          project=lambda body: {'picked': body['1']['wheelhouse'][1:4]})
    full = cache.decode(encoded, keys=keys)
    assert len(calls) == 1
    assert second == {'picked': tracks[1:4]}
    assert full == {key: value[key] for key in keys}
    full['all']['wheelhouse'][0]['musical']['provenance'][0]['recording'] = 'changed'
    assert full['1']['wheelhouse'][0]['musical']['provenance'][0]['recording'] == 0
    assert cache.decode(encoded, keys=keys)['all']['wheelhouse'] == tracks


def test_cache_identity_tracks_payload_and_requested_fields_and_keeps_only_one(monkeypatch):
    read, calls = json_cache._read, []

    def observed(*args):
        calls.append(args)
        return read(*args)

    monkeypatch.setattr(json_cache, '_read', observed)
    cache = json_cache.DecodeCache()
    old, new = '{"1": [1], "2": [2]}', '{"1": [3], "2": [4]}'
    assert cache.decode(old, keys={'1'}) == {'1': [1]}
    assert cache.decode(old, keys={'2'}) == {'2': [2]}
    assert cache.decode(new, keys={'2'}) == {'2': [4]}
    assert cache.decode(old, keys={'2'}) == {'2': [2]}
    assert len(calls) == 4
    assert cache.decode(old, keys={'2'}) == {'2': [2]}
    assert len(calls) == 4
    with pytest.raises(ValueError):
        cache.decode('invalid')
    assert cache.decode(old, keys={'2'}) == {'2': [2]}
    assert len(calls) == 5  # A failed parse cannot replace the previous good value.


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
def test_stable_projection_is_cached_detached_and_replaced_with_its_document(format):
    cache = json_cache.DecodeCache()
    tracks = [{'key': str(i), 'description': 'evidence ' * 1500,
               'musical': {'source': i}} for i in range(8)]
    calls = []
    def project(value):
        calls.append(True)
        return {'picked': value['all']['wheelhouse'][:2]}
    old = _encoded(format, {'all': {'wheelhouse': tracks}})
    first = cache.decode(old, project=project, cache_projection=True)
    first['picked'][0]['musical']['source'] = 'local edit'
    assert cache.decode(old, project=project, cache_projection=True) == {'picked': tracks[:2]}
    assert len(calls) == 1
    new = _encoded(format, {'all': {'wheelhouse': tracks[1:]}})
    assert cache.decode(new, project=project, cache_projection=True) == {'picked': tracks[1:3]}
    assert len(calls) == 2


def test_profile_free_v2_documents_are_detached_and_reject_dangling_references():
    value = {'tracks': [{'key': str(i), 'order': {'relax': [i, i / 1000]},
                         'supported': ['relax']} for i in range(2000)]}
    encoded = json_cache.encode(value)
    assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    cache = json_cache.DecodeCache()
    first = cache.decode(encoded)
    first['tracks'][0]['order']['relax'].append('local edit')
    assert cache.decode(encoded) == json_cache.decode(encoded) == value
    dangling = json.dumps({json_cache.MARKER: json_cache.FORMAT,
        'json': json_cache._pack('{"tracks":[{"musical":{"profile":0}}]}'),
        'profiles': json_cache._pack('[]')})
    with pytest.raises(ValueError, match='Invalid compressed recommendation cache profile'):
        json_cache.decode(dangling)


@pytest.mark.parametrize('group_pools', [False, True])
def test_logical_budget_counts_each_copy_and_keeps_exact_detached_profiles(group_pools):
    profile = {'captured': ['東京\\\"' * 300], 'provenance': {'source': 'original'}}
    tracks = [{'key': str(i), 'musical': profile, '東京\\\"': [None, False, True, 0, .1]}
              for i in range(100)]
    value = {'all': {'wheelhouse': tracks}, 'menus': [{'tracks': tracks[:3]}]}
    encoded = json_cache.encode(value, group_pools=group_pools)
    size = len(json.dumps(value, separators=(',', ':')).encode())
    with pytest.raises(ValueError, match='logical byte budget'):
        json_cache.decode(encoded, max_bytes=size - 1)
    restored = json_cache.decode(encoded, max_bytes=size)
    assert restored == value
    restored['all']['wheelhouse'][0]['musical']['captured'].append('local edit')
    assert restored['all']['wheelhouse'][1]['musical'] == profile
    assert restored['menus'][0]['tracks'][0]['musical'] == profile
    # A bounded consumer may select rows before reconstructing their evidence.
    selected = json_cache.decode(encoded, project=lambda body: body['menus'], max_bytes=20000)
    assert selected == value['menus']


def test_profile_reconstruction_is_cancellable_between_copies(monkeypatch):
    profile = json.dumps({'captured': [1, 2, 3]})
    body = [{'musical': {'profile': 0}} for _ in range(1000)]
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 20:
            raise WorkClosing('cancel reconstruction')

    monkeypatch.setattr(json_cache, 'check_cancellable_work', cancel)
    with pytest.raises(WorkClosing, match='cancel reconstruction'):
        json_cache._restore_profiles(body, [profile])
    assert calls == 20


def test_concurrent_miss_parses_once_and_a_cancelled_waiter_does_not_cancel_owner(monkeypatch):
    cache = json_cache.DecodeCache()
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    read, calls = json_cache._read, []

    def slow_read(*args):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return read(*args)

    def cancelled_waiter():
        checks = 0
        def stop():
            nonlocal checks
            checks += 1
            if checks > 1:
                waiting.set()
                return True
            return False
        with cancellable_work(stop):
            return cache.decode('{"items": [1]}')

    monkeypatch.setattr(json_cache, '_read', slow_read)
    with ThreadPoolExecutor(3) as threads:
        owner = threads.submit(cache.decode, '{"items": [1]}')
        try:
            assert entered.wait(2)
            follower = threads.submit(cache.decode, '{"items": [1]}')
            cancelled = threads.submit(cancelled_waiter)
            assert waiting.wait(2)
            with pytest.raises(WorkClosing):
                cancelled.result(2)
            assert not owner.done()
        finally:
            release.set()
        first, second = owner.result(2), follower.result(2)
    assert len(calls) == 1 and first == second == {'items': [1]}
    first['items'].append(2)
    assert second == {'items': [1]}
