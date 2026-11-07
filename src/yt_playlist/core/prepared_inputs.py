"""Typed accepted content inputs, independent of subsequent provider updates."""
import math


SCOPES = ('library', 'discovered')
AUDIO = ('bpm', 'energy', 'danceability', 'mood_happy', 'mood_sad', 'mood_relaxed',
         'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity', 'music_key', 'music_scale')
FIELDS = tuple(field for scope in SCOPES for field in (
    scope + '_present', scope + '_genre', scope + '_year', scope + '_audio_fields',
    *(scope + '_' + audio for audio in AUDIO)))
COLUMNS = (*FIELDS, 'input_revision', 'digest', 'built_at')


def flatten(payload):
    if not isinstance(payload, dict) or set(payload) != set(SCOPES):
        raise ValueError('Prepared inputs require explicit library and discovered cohorts')
    values = []
    for scope in SCOPES:
        features = payload[scope]
        if features is None:
            values.extend((0, None, None, 0, *(None for _ in AUDIO)))
            continue
        if not isinstance(features, dict) or set(features) != {'genre', 'year', 'audio'}:
            raise ValueError('Unsupported prepared input fields')
        audio = features['audio']
        if not isinstance(audio, dict) or set(audio) - set(AUDIO):
            raise ValueError('Unsupported prepared audio fields')
        if any(features[name] is not None and not isinstance(features[name], str) for name in ('genre', 'year')):
            raise ValueError('Prepared genre and year must be text or absent')
        for field, value in audio.items():
            if field in AUDIO[-2:]:
                if value is not None and not isinstance(value, str):
                    raise ValueError('Prepared musical key and scale must be text or absent')
            elif value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('Prepared audio measurements must be finite numbers or absent')
        mask = sum(1 << i for i, field in enumerate(AUDIO) if field in audio)
        values.extend((1, features['genre'], features['year'], mask, *(audio.get(field) for field in AUDIO)))
    return tuple(values)


def payload(row):
    result = {}
    for scope in SCOPES:
        result[scope] = ({'genre': row[scope + '_genre'], 'year': row[scope + '_year'],
                          'audio': {field: row[scope + '_' + field] for i, field in enumerate(AUDIO)
                                    if row[scope + '_audio_fields'] & (1 << i)}}
                         if row[scope + '_present'] else None)
    return result


def upsert(conn, key, inputs, digest, now, revision):
    upsert_values(conn, key, flatten(inputs), digest, now, revision, empty=not any(inputs.values()))


def upsert_values(conn, key, fields, digest, now, revision, *, empty):
    """Persist validated columns against their current relational owner."""
    values = (*fields, revision, digest, now)
    row = conn.execute('SELECT r.id FROM recordings r WHERE r.identity_key=? AND NOT EXISTS('
                       'SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id)', (key,)).fetchone()
    columns = ','.join(COLUMNS)
    updates = ','.join(f'{field}=excluded.{field}' for field in COLUMNS)
    if row:
        conn.execute(f'INSERT INTO prepared_recording_inputs(recording_id,{columns}) VALUES ('
                     + ','.join('?' for _ in range(1 + len(values))) + ') '
                     f'ON CONFLICT(recording_id) DO UPDATE SET {updates}', (row[0], *values))
        # An admitted owner can replace its disposable accepted retraction.
        conn.execute("DELETE FROM unresolved_preparation_inputs WHERE raw_key=? AND reason='empty_retraction'", (key,))
    else:
        reason = ('empty_retraction' if empty else
                  'retired_owner' if conn.execute('SELECT 1 FROM recordings WHERE identity_key=?', (key,)).fetchone()
                  else 'missing_owner')
        conn.execute(f'INSERT INTO unresolved_preparation_inputs(raw_key,reason,{columns}) VALUES ('
                     + ','.join('?' for _ in range(2 + len(values))) + ') '
                     f'ON CONFLICT(raw_key) DO UPDATE SET reason=excluded.reason,{updates}', (key, reason, *values))


def byte_bound(alias='p'):
    """Bound one decoded row, including text and dictionary overhead, in SQL."""
    text = [scope + '_' + field for scope in SCOPES for field in ('genre', 'year', *AUDIO[-2:])]
    return '4096+' + '+'.join(f'COALESCE(length(CAST({alias}.{field} AS BLOB))*4,0)' for field in text)
