"""Historical v1 envelopes for retained-state fixtures; no value-sharing writer."""
import base64
import zlib

from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.policy_config import references


def legacy_projection(run_id, value):
    raw = canonical(value)
    return {'run_id': run_id, 'codec': 'json-zlib-v1', 'expanded_bytes': len(raw),
            'context': value.get('context') or value.get('choice_context') or {},
            'policies': [{'model_version': version} for version in sorted(set(references(value)))],
            'data': base64.b64encode(zlib.compress(raw)).decode('ascii')}
