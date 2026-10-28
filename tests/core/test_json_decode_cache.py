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
