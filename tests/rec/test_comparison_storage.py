"""Complete paired pools fit by sharing values, without merging policy variants."""
import base64
import json
import random
import zlib

import pytest

from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec import comparison_storage as storage
from yt_playlist.repos.policy_storage import references


def envelope(codec, value, expanded_bytes, **extra):
    raw = canonical(value)
    return {'codec': codec, 'expanded_bytes': expanded_bytes, 'packed_bytes': len(raw),
            'data': base64.b64encode(zlib.compress(raw)).decode(), **extra}


def test_distant_complete_pool_variants_share_metadata_without_losing_scores_or_dependencies():
    rng = random.Random(138)
    tracks = [{'key': f'recording:{i}', 'metadata': base64.b64encode(rng.randbytes(1500)).decode(),
               'musical': {'score': rng.random(), 'support': .75}, 'genre': 'House'} for i in range(128)]
    pools = {str(preset): [{**track, 'score': rng.random(), 'pool_policy': f'{preset:016x}'}
                          for track in tracks] for preset in range(12)}
    data = {'bundles': {'_vibes': {'pools': pools}, '_radio': {'members': {
        'style': [track['key'] for track in tracks]}}}, 'context': {'artifact': 'held-context'}}
    raw = canonical(data)
    encoded = storage.encode(138, data)
    payload = json.loads(encoded)
    assert payload['codec'] == storage.CODEC
    # Metadata reappears beyond zlib's local window; sharing must save the
    # repeated distant copies, including when each pool has different scores.
    assert len(encoded) < len(base64.b64encode(zlib.compress(raw, level=3))) / 4
    assert canonical(storage.decode(payload)) == raw
    assert set(references(data)) <= set(references(payload))
    assert payload['context'] == data['context']
    assert payload['expanded_bytes'] == len(raw)


def test_shared_values_retain_json_types_signed_zero_and_independent_mutation():
    values = [True, 1, 1.0, False, 0, 0.0, -0.0, None, '1', [], {}, {'$ref': 1}, [0]]
    data = {'menus': [{'tracks': values}] * 3}
    packed = json.loads(storage._pack(data))
    restored = storage._unpack(packed, len(canonical(data)))
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


def test_expansion_is_bounded_before_restoring_shared_values(monkeypatch):
    nodes = ['leaf']
    for index in range(1, 30):
        nodes.append([index - 1, index - 1])
    nodes.append({'root': len(nodes) - 1})
    payload = envelope(storage.CODEC, {'nodes': nodes, 'root': len(nodes) - 1}, 100)
    with pytest.raises(ValueError, match='expanded storage budget'):
        storage.decode(payload)
    data = {'menus': [{'metadata': 'complete'}] * 100}
    packed = json.loads(storage._pack(data))
    monkeypatch.setattr(storage, 'MAX_EXPANDED_BYTES', len(canonical(packed)) + 1)
    with pytest.raises(ValueError, match='storage budget'):
        storage.encode(138, data)


def test_plain_fallback_bounds_interning_and_keeps_complete_data(monkeypatch):
    data = {'menus': [{'id': str(i), 'score': i / 10} for i in range(10)]}
    monkeypatch.setattr(storage, 'MAX_NODES', 2)
    payload = json.loads(storage.encode(138, data))
    assert payload['codec'] == storage.LEGACY_CODEC
    assert storage.decode(payload) == data


def test_sharing_does_not_swallow_shutdown_cancellation(monkeypatch):
    def stop():
        raise WorkClosing('shutdown')
    monkeypatch.setattr(storage, 'check_cancellable_work', stop)
    with pytest.raises(WorkClosing):
        storage.encode(138, {'same': ['value'] * 2000})
