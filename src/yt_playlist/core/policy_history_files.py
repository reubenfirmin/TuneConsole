"""Database-free bytes for an archived policy and its exact shared base."""
import hashlib
import json

from yt_playlist.core import fao
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.policy_paths import record_path

FORMAT = 'recommendation-policy-history-1'
MAX_BYTES = 8 * 1024 * 1024


def encode(version, value):
    raw = canonical({'format': FORMAT, 'version': version, 'value': value})
    if len(raw) > MAX_BYTES:
        raise fao.FileBudgetExceeded('Historical policy exceeds its file byte budget')
    return raw


def read(root, version, reference):
    raw = fao.read(record_path(root, reference['id']), max_bytes=MAX_BYTES).bytes
    if len(raw) != reference['byte_size'] or hashlib.sha256(raw).hexdigest() != reference['content_digest']:
        raise fao.CorruptFile('Historical policy differs from its committed configuration')
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'format', 'version', 'value'}
            or value['format'] != FORMAT or value['version'] != version or canonical(value) != raw
            or not isinstance(value['value'], dict) or value['value'].get('state') != 'archived'):
        raise fao.CorruptFile('Invalid historical policy file')
    return value['value']
