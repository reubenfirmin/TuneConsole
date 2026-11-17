"""Identity of a displayed Home choice, independent of acquisition and assembly."""
import hashlib
import json


def experiment_id(proto):
    choice = {'lane': proto['lane'], 'label': proto.get('label'), 'note': proto.get('note'),
              'genres': sorted(set(proto.get('genres') or []))}
    return hashlib.sha256(json.dumps(choice, sort_keys=True).encode()).hexdigest()[:24]
