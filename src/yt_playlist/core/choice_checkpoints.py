"""Validate immutable correction captures independently of their learning consumers."""
import hashlib

from .evidence_contract import canonical

CORRECTION_CONTRACT = 'choice-correction-v2'
MAX_CORRECTION_BYTES = 256 * 1024


def checkpoint(ledger, identity):
    return validate(ledger.get('checkpoint', identity), identity)


def validate(value, identity):
    """The same correction contract applies to exact reads and retention scans."""
    if (not value or value.get('schema') != 1 or value.get('contract') not in {CORRECTION_CONTRACT, 'choice-correction-v1'}
            or value.get('source_context') != identity or not value.get('watermark')
            or not isinstance(value.get('context'), dict)):
        return None
    encoded = canonical(value['context'])
    if (len(encoded) > MAX_CORRECTION_BYTES
            or hashlib.sha256(encoded).hexdigest() != value.get('digest')):
        return None
    return value
