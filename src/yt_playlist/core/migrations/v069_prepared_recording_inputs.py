"""Accepted encoder inputs use typed canonical ownership, including raw retractions."""
import hashlib
import json
import math

from .runner import MigrationError


# Frozen v69 input vocabulary. Current producers do not determine historical DDL.
SCOPES = ('library', 'discovered')
AUDIO = ('bpm', 'energy', 'danceability', 'mood_happy', 'mood_sad', 'mood_relaxed',
         'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity', 'music_key', 'music_scale')
FIELDS = tuple(field for scope in SCOPES for field in (
    scope + '_present', scope + '_genre', scope + '_year', scope + '_audio_fields',
    *(scope + '_' + audio for audio in AUDIO)))


def statements():
    fields, checks = [], []
    for scope in SCOPES:
        fields.extend((f'{scope}_present INTEGER NOT NULL CHECK({scope}_present IN (0,1))',
                       f'{scope}_genre TEXT', f'{scope}_year TEXT',
                       f"{scope}_audio_fields INTEGER NOT NULL CHECK(typeof({scope}_audio_fields)='integer' "
                       f'AND {scope}_audio_fields BETWEEN 0 AND 4095)'))
        for field in AUDIO:
            column = scope + '_' + field
            fields.append(f'{column} TEXT' if field in AUDIO[-2:] else
                          f"{column} REAL CHECK({column} IS NULL OR (typeof({column}) IN ('real','integer') "
                          f'AND abs({column})<=1.7976931348623157e308))')
        empty = ' AND '.join(f'{scope}_{field} IS NULL' for field in ('genre', 'year', *AUDIO))
        checks.append(f'CHECK({scope}_present=1 OR ({scope}_audio_fields=0 AND {empty}))')
        for i, field in enumerate(AUDIO):
            checks.append(f'CHECK(({scope}_audio_fields & {1 << i})<>0 OR {scope}_{field} IS NULL)')
    fields.extend(("input_revision INTEGER NOT NULL CHECK(typeof(input_revision)='integer' AND input_revision>=0)",
                   "digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*')",
                   "built_at REAL NOT NULL CHECK(typeof(built_at) IN ('real','integer') "
                   'AND abs(built_at)<=1.7976931348623157e308)'))
    body = ','.join((*fields, *checks))
    yield ('CREATE TABLE prepared_recording_inputs (recording_id INTEGER PRIMARY KEY NOT NULL REFERENCES recordings(id),'
           + body + ') WITHOUT ROWID')
    yield 'CREATE INDEX ix_prepared_recording_revision ON prepared_recording_inputs(input_revision)'
    yield ('CREATE INDEX ix_group_library_inputs ON prepared_recording_inputs(recording_id) '
           'WHERE library_present=1')
    yield ("CREATE TABLE unresolved_preparation_inputs (raw_key TEXT PRIMARY KEY NOT NULL,reason TEXT NOT NULL "
           "CHECK(reason IN ('empty_retraction','missing_owner','retired_owner'))," + body + ') WITHOUT ROWID')
    yield 'CREATE INDEX ix_unresolved_preparation_revision ON unresolved_preparation_inputs(input_revision)'
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        yield (f'CREATE TRIGGER group_clock_prepared_recording_inputs_{event.lower()} AFTER {event} '
               'ON prepared_recording_inputs BEGIN UPDATE rec_group_clock SET serial=serial+1 WHERE id=1; END')
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER prepared_recording_inputs_owner_{event.lower()} BEFORE {event} '
               'ON prepared_recording_inputs WHEN EXISTS(SELECT 1 FROM recording_redirects '
               'WHERE retired_id=NEW.recording_id) '
               "BEGIN SELECT RAISE(ABORT,'Prepared input owner is retired'); END")
        yield (f'CREATE TRIGGER retire_prepared_recording_inputs_{event.lower()} AFTER {event} '
               'ON recording_redirects BEGIN DELETE FROM prepared_recording_inputs '
               'WHERE recording_id=NEW.retired_id; END')


def _flatten(raw):
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


def apply(conn, context):
    # Validate every original before removing the old owner or its indexes.
    for key, raw, digest, captured, revision in conn.execute('SELECT * FROM rec_prepared_content'):
        try:
            _flatten(raw)
            if (hashlib.sha256(raw.encode()).hexdigest() != digest or type(revision) is not int
                    or revision < 0 or not isinstance(captured, (int, float)) or not math.isfinite(captured)):
                raise ValueError('Invalid input digest, revision or capture time')
        except (ValueError, TypeError) as exc:
            raise MigrationError(f'Invalid prepared input {key!r}: {exc}') from exc
    conn.execute('DROP INDEX ix_group_library_inputs')
    for sql in statements():
        if not sql.startswith('CREATE TRIGGER'):
            conn.execute(sql)
    columns = ','.join((*FIELDS, 'input_revision', 'digest', 'built_at'))
    for key, raw, digest, captured, revision in conn.execute('SELECT * FROM rec_prepared_content ORDER BY rowid'):
        values, empty = _flatten(raw)
        row = conn.execute('SELECT r.id,d.retired_id FROM recordings r LEFT JOIN recording_redirects d '
                           'ON d.retired_id=r.id WHERE r.identity_key=?', (key,)).fetchone()
        data = (*values, revision, digest, captured)
        if row and row[1] is None:
            conn.execute(f'INSERT INTO prepared_recording_inputs(recording_id,{columns}) VALUES ('
                         + ','.join('?' for _ in range(1 + len(data))) + ')', (row[0], *data))
        else:
            reason = 'empty_retraction' if empty else 'retired_owner' if row else 'missing_owner'
            conn.execute(f'INSERT INTO unresolved_preparation_inputs(raw_key,reason,{columns}) VALUES ('
                         + ','.join('?' for _ in range(2 + len(data))) + ')', (key, reason, *data))
    conn.execute('DROP TABLE rec_prepared_content')
    # Moving accepted rows is not an input change. Install invalidation only
    # after the copy so existing publication serials remain valid on upgrade.
    for sql in statements():
        if sql.startswith('CREATE TRIGGER'):
            conn.execute(sql)


def validate(conn):
    from .current import _tokens
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible prepared input structure: {name}')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='rec_prepared_content'").fetchone():
        raise MigrationError('Superseded prepared input store remains')
