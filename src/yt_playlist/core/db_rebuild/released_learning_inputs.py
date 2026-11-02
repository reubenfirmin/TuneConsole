"""Read the released model-input vocabulary without executing historical upgrades."""
import hashlib
import json
import math

from .. import embeddings
from ..schema_contract import MigrationError


AXIS_TABLES = ('rec_weights','rec_lean','home_hidden_facet','rec_theme','rec_play_grad','rec_grad_log')
AXIS_RENAMES = {'rec_play_grad':'play_gradient_checkpoints','rec_grad_log':'recommendation_gradient_events'}
SOURCES = {name: (*description, 'identity_key' if description[0] == 'recording' else 'artist')
           for name, description in embeddings.SPACES.items()}
SCOPES = ('library', 'discovered')
AUDIO = ('bpm', 'energy', 'danceability', 'mood_happy', 'mood_sad', 'mood_relaxed',
         'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity', 'music_key', 'music_scale')
FIELDS = tuple(field for scope in SCOPES for field in (
    scope + '_present', scope + '_genre', scope + '_year', scope + '_audio_fields',
    *(scope + '_' + audio for audio in AUDIO)))


def resolve_mood(conn, key):
    direct = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    candidates = [direct[0]] if direct else [row[0] for row in conn.execute(
        'SELECT recording_id FROM recording_aliases WHERE alias=?', (key,))]
    resolved = set()
    for recording_id in candidates:
        seen = set()
        while recording_id not in seen:
            seen.add(recording_id)
            target = conn.execute('SELECT recording_id FROM recording_redirects WHERE retired_id=?',
                                  (recording_id,)).fetchone()
            if target is None:
                resolved.add(recording_id)
                break
            recording_id = target[0]
        else:
            raise MigrationError('Cyclic recording redirect in mood seed; original event retained')
    if len(resolved) == 1:
        return resolved.pop(), None
    return None, 'ambiguous_alias' if resolved else 'unknown_recording'


def flatten(raw):
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != set(SCOPES):
        raise ValueError('Expected explicit library/discovery cohorts')
    values = []
    for scope in SCOPES:
        features = payload[scope]
        if features is None:
            values.extend((0, None, None, 0, *(None for _ in AUDIO)))
            continue
        if not isinstance(features, dict) or set(features) != {'genre', 'year', 'audio'}:
            raise ValueError('Unknown prepared fields')
        audio = features['audio']
        if not isinstance(audio, dict) or set(audio) - set(AUDIO):
            raise ValueError('Unknown prepared audio fields')
        if any(features[name] is not None and not isinstance(features[name], str) for name in ('genre', 'year')):
            raise ValueError('Invalid prepared category')
        for field, value in audio.items():
            if field in AUDIO[-2:]:
                if value is not None and not isinstance(value, str):
                    raise ValueError('Invalid prepared musical key/scale')
            elif value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('Invalid prepared audio measurement')
        mask = sum(1 << i for i, field in enumerate(AUDIO) if field in audio)
        values.extend((1, features['genre'], features['year'], mask, *(audio.get(field) for field in AUDIO)))
    return tuple(values), not any(payload.values())


def basis(conn, source, kind, space, settings):
    known = embeddings.version(source, settings)
    if known is not None:
        return known
    digest = hashlib.sha256(source.encode())
    for key, vector in conn.execute(f'SELECT {SOURCES[source][2]},vec FROM {source} ORDER BY rowid'):
        digest.update(json.dumps(key).encode())
        digest.update(vector)
    return 'legacy-unversioned:' + digest.hexdigest()


def owner(conn, kind, key):
    if kind == 'recording':
        matches = [row[0] for row in conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,))]
        redirects = 'recording_redirects'
    else:
        redirects = 'artist_redirects'
        if key.startswith('@artist:') and key[8:].isdigit():
            matches = [row[0] for row in conn.execute('SELECT id FROM artists WHERE id=?', (int(key[8:]),))]
        else:
            matches = [row[0] for row in conn.execute('SELECT DISTINCT artist_id FROM artist_aliases '
                                                    'WHERE alias=?', (key,))]
    if not matches:
        return None, 'missing_owner'
    if len(matches) != 1:
        return None, 'ambiguous_owner'
    if conn.execute(f'SELECT 1 FROM {redirects} WHERE retired_id=?', (matches[0],)).fetchone():
        return None, 'retired_owner'
    return matches[0], None
