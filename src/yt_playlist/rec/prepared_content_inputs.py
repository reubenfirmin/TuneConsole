"""Build accepted model inputs from detached catalog facts."""
import hashlib
import json


def capture(pending, facts, discovered):
    result = []
    for key, revision in pending:
        genre, year = facts['content'].get(key, (None, None))
        year = year[:4] if year and year[:4].isdigit() else None
        discovery = discovered.get(key)
        payload = {
            'library': ({'genre': genre, 'year': year, 'audio': facts['audio'].get(key, {})}
                        if key in facts['library'] else None),
            'discovered': ({'genre': discovery.get('genre'), 'year': discovery.get('year'),
                            'audio': discovery.get('audio', {})} if discovery else None),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        result.append((key, revision, raw, digest, facts['previous'].get(key) != digest))
    return result
