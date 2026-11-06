"""Persisted evidence bytes and correction limits survive helper extraction."""
import hashlib
import json

import pytest

from yt_playlist.core import choice_checkpoints, evidence_contract, fao
from yt_playlist.core.evidence_ledger import EvidenceLedger, canonical


VALUE = {'z': [1, -0.0, True, None, {'$artifact': 'child'}], 'é': '雪',
         'a': {'nested': [{'$artifact': 'first'}, {'$artifact': 'ignored', 'extra': 1}]}}
# Captured from the installed encoder before moving it out of the ledger.
ENCODED = (br'{"a":{"nested":[{"$artifact":"first"},{"$artifact":"ignored","extra":1}]},'
           br'"z":[1,-0.0,true,null,{"$artifact":"child"}],"\u00e9":"\u96ea"}')
DIGEST = '1873f2d0656efe59c883386342e16c11cfc366dc6fb0bf24b4af3ce1fc6486c6'


def test_frozen_bytes_hash_and_existing_ledger_record_remain_exact(tmp_path):
    assert evidence_contract.canonical(VALUE) == canonical(VALUE) == ENCODED
    assert hashlib.sha256(evidence_contract.canonical(VALUE)).hexdigest() == DIGEST
    ledger = EvidenceLedger(tmp_path, 1_000_000)
    fao.write_bytes(ledger._path('response', 'frozen'), ENCODED)
    assert ledger.get('response', 'frozen') == VALUE
    assert not ledger.put('response', 'frozen', VALUE)
    assert fao.read_bytes(ledger._path('response', 'frozen')) == ENCODED


def test_recursive_references_preserve_order_duplicates_and_exact_reference_shape():
    assert list(evidence_contract.artifact_components(VALUE)) == ['child', 'first']
    value = [{'$artifact': 'same'}, {'more': [{'$artifact': 'same'}]},
             {'$artifact': 'ordinary-field', 'more': {'$artifact': 'nested'}},
             ({'$artifact': 'tuple-is-not-a-json-array'},)]
    assert list(evidence_contract.artifact_components(value)) == ['same', 'same', 'nested']


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_evidence_remains_rejected(value):
    with pytest.raises(ValueError):
        evidence_contract.canonical({'value': value})


@pytest.mark.parametrize('contract', ['choice-correction-v1', 'choice-correction-v2'])
@pytest.mark.parametrize('damage', [None, 'digest', 'source', 'watermark', 'schema', 'contract'])
def test_both_persisted_correction_contracts_keep_validation(contract, damage):
    value = {'schema': 1, 'contract': contract, 'source_context': 'original',
             'watermark': 'committed', 'context': VALUE, 'digest': DIGEST}
    if damage:
        key, replacement = {
            'digest': ('digest', '0' * 64), 'source': ('source_context', 'different'),
            'watermark': ('watermark', ''), 'schema': ('schema', 2),
            'contract': ('contract', 'unrecognized'),
        }[damage]
        value[key] = replacement
    assert choice_checkpoints.validate(value, 'original') == (None if damage else value)


@pytest.mark.parametrize('contract', ['choice-correction-v1', 'choice-correction-v2'])
@pytest.mark.parametrize('extra', [0, 1])
def test_correction_byte_boundary_is_inclusive_and_unchanged(contract, extra):
    assert choice_checkpoints.MAX_CORRECTION_BYTES == 256 * 1024
    context = {'payload': 'x' * (256 * 1024 - 14 + extra)}
    encoded = json.dumps(context, separators=(',', ':')).encode()
    assert len(encoded) == 256 * 1024 + extra
    value = {'schema': 1, 'contract': contract, 'source_context': 'original',
             'watermark': 'committed', 'context': context,
             'digest': hashlib.sha256(encoded).hexdigest()}
    assert choice_checkpoints.validate(value, 'original') == (None if extra else value)
