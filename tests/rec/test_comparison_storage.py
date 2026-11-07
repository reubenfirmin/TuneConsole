"""Historical paired projections keep bounded, independent decoded values."""
import base64
import zlib

import pytest

from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec import comparison_storage as storage


def envelope(codec, value, expanded_bytes, **extra):
    raw = canonical(value)
    return {'codec': codec, 'expanded_bytes': expanded_bytes, 'packed_bytes': len(raw),
            'data': base64.b64encode(zlib.compress(raw)).decode(), **extra}


def test_shared_values_retain_json_types_signed_zero_and_independent_mutation():
    values = [True, 1, 1.0, False, 0, 0.0, -0.0, None, '1', [], {}, {'$ref': 1}, [0]]
    data = {'menus': [{'tracks': values}] * 3}
    # Explicit historical node indexes retain distinct scalar encodings and
    # reuse the same track-list node in three menus without a runtime encoder.
    nodes = [True, 1, 1.0, False, 0, 0.0, -0.0, None, '1', [], {}, {'$ref': 1}, [4],
             list(range(13)), {'tracks': 13}, [14, 14, 14], {'menus': 15}]
    restored = storage.decode(envelope(storage.CODEC, {'nodes': nodes, 'root': 16}, len(canonical(data))))
    assert canonical(restored) == canonical(data)
    assert restored['menus'][0] is not restored['menus'][1]
    assert restored['menus'][0]['tracks'] is not restored['menus'][1]['tracks']
    restored['menus'][0]['tracks'][11]['$ref'] = 'changed'
    restored['menus'][0]['tracks'].append('new')
    assert restored['menus'][1]['tracks'] == values
    assert values[11] == {'$ref': 1}


def test_both_previous_storage_formats_remain_readable():
    data = {'items': [{'id': 'old', 'score': .123}], 'context': {'artifact': 'held'}}
    previous = envelope(storage.LEGACY_CODEC, data, len(canonical(data)))
    previous.pop('packed_bytes')
    assert storage.decode(previous) == data
    assert storage.decode({'run_id': 138, 'data': data}) == data


@pytest.mark.parametrize('packed', [
    {'nodes': [{'self': 0}], 'root': 0},
    {'nodes': [{'forward': 1}, 'value'], 'root': 0},
    {'nodes': ['value', {'negative': -1}], 'root': 1},
    {'nodes': ['value', {'boolean-reference': True}], 'root': 1},
    {'nodes': ['value'], 'root': True},
    {'nodes': ['scalar-root'], 'root': 0},
])
def test_invalid_node_references_and_roots_are_rejected(packed):
    with pytest.raises(ValueError):
        storage.decode(envelope(storage.CODEC, packed, 20))


def test_expansion_is_bounded_before_restoring_shared_values():
    nodes = ['leaf']
    for index in range(1, 30):
        nodes.append([index - 1, index - 1])
    nodes.append({'root': len(nodes) - 1})
    payload = envelope(storage.CODEC, {'nodes': nodes, 'root': len(nodes) - 1}, 100)
    with pytest.raises(ValueError, match='expanded storage budget'):
        storage.decode(payload)


def test_decoding_does_not_swallow_shutdown_cancellation(monkeypatch):
    def stop():
        raise WorkClosing('shutdown')
    monkeypatch.setattr(storage, 'check_cancellable_work', stop)
    packed = {'nodes': ['value', [0] * 2000, {'same': 1}], 'root': 2}
    with pytest.raises(WorkClosing):
        storage.decode(envelope(storage.CODEC, packed, len(canonical({'same': ['value'] * 2000}))))
