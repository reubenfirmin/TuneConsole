"""Prepare ordered legacy conflict evidence without choosing its runtime owner.

Candidate bodies can contain arbitrarily many items in the installed format.
The byte budget bounds decoding outside SQLite; the checkpoint bounds individual
candidate inserts inside each transaction. Saved resolutions remain independent
decisions, even when their value equals one of the offered candidates.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from .. import fao
import time

from .. import metadata_legacy
from ..schema_contract import MigrationError
from ..policy_documents import _publish_encoded
from .rows import Job, main as run


MAX_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * MAX_BYTES + 16384
HEAD = 'prepared_metadata_conflict_generations'
CANDIDATES = 'prepared_metadata_conflict_candidates'
RESOLUTIONS = 'prepared_metadata_conflict_resolutions'
CURRENT = 'prepared_metadata_conflict_heads'
COMPATIBILITY = 'prepared_metadata_conflict_compatibility'
TEXT_COLUMNS = ('field', 'candidates', 'resolved_value', 'input_video_id', 'field_scope')
COLUMNS = ('id', 'track_id', 'field', 'candidates', 'resolved', 'resolved_value', 'updated_at',
           'recording_id', 'input_video_id', 'field_scope')
STATUSES = {0: 'unresolved', 1: 'user_resolved', 2: 'provider_converged'}
VALUE_COLUMNS = ('value_kind', 'text_value', 'number_value', 'genre_key', 'unit', 'resolution_reason')
VALUE_SQL = '''value_kind TEXT CHECK(value_kind IN ('value','null','blank','unresolved','withdrawn')),
    text_value TEXT, number_value, genre_key TEXT, unit TEXT, resolution_reason TEXT,
    CHECK(number_value IS NULL OR (typeof(number_value) IN ('integer','real') AND
          number_value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308))'''


def _unused(*args):
    raise MigrationError('Metadata conflicts require their bounded candidate worker')


DEFINITION = Job('metadata_conflicts', 79, 79, {'enrichment_conflict': ('id',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        conflict_id INTEGER NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {MAX_ARCHIVE_BYTES}),
        entry_id INTEGER, recording_id INTEGER, input_video_id TEXT, field TEXT, field_scope TEXT,
        disposition TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('unresolved','user_resolved','provider_converged')),
        updated_at REAL, body_kind TEXT NOT NULL CHECK(body_kind IN ('list','malformed','not_list')),
        candidates_count INTEGER NOT NULL CHECK(candidates_count>=0),
        PRIMARY KEY(conflict_id,generation)) WITHOUT ROWID'''),
    (CANDIDATES, f'''CREATE TABLE {CANDIDATES} (
        conflict_id INTEGER NOT NULL, generation INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK(position>=0), raw_candidate TEXT NOT NULL CHECK(json_valid(raw_candidate)),
        provider TEXT, disposition TEXT NOT NULL CHECK(disposition IN ('offered','invalid_candidate','unsupported_field','unproven_subject')),
        {VALUE_SQL},
        PRIMARY KEY(conflict_id,generation,position),
        FOREIGN KEY(conflict_id,generation) REFERENCES {HEAD}(conflict_id,generation)) WITHOUT ROWID'''),
    (RESOLUTIONS, f'''CREATE TABLE {RESOLUTIONS} (
        conflict_id INTEGER NOT NULL, generation INTEGER NOT NULL, raw_value TEXT,
        decision_reason TEXT CHECK(decision_reason IN ('unrecorded_legacy_resolution','unsupported_field','unproven_subject','unsupported_legacy_value')),
        {VALUE_SQL},
        PRIMARY KEY(conflict_id,generation),
        FOREIGN KEY(conflict_id,generation) REFERENCES {HEAD}(conflict_id,generation)) WITHOUT ROWID'''),
    (CURRENT, f'''CREATE TABLE {CURRENT} (
        conflict_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(conflict_id,generation) REFERENCES {HEAD}(conflict_id,generation))'''),
), _unused, _unused, dependencies=('catalog_entries', 'recordings', 'youtube_items', 'genre_catalog', 'genre_alias'),
    additive_destinations=((COMPATIBILITY, f'''CREATE TABLE {COMPATIBILITY} (
        conflict_id INTEGER NOT NULL,generation INTEGER NOT NULL,
        track_id INTEGER,field TEXT,resolved_value TEXT,recording_id INTEGER,input_video_id TEXT,field_scope TEXT,
        non_list_body TEXT,
        PRIMARY KEY(conflict_id,generation),
        FOREIGN KEY(conflict_id,generation) REFERENCES {HEAD}(conflict_id,generation)) WITHOUT ROWID'''),))


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Metadata conflict preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'metadata-conflicts'


def read_original(row):
    with fao.open_reader(row['filename']) as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared metadata conflict original does not match its digest')
    return json.loads(encoded)


def _select(conn, current):
    if current.owner is None:
        row = conn.execute(f'SELECT source_key FROM {DEFINITION.changes} ORDER BY source_key LIMIT 1').fetchone()
        return json.loads(row[0])[0] if row else None
    cursor = json.loads(current.cursor) if current.cursor else None
    if cursor and len(cursor) == 3:
        return cursor[0]
    highwater = conn.execute(f'SELECT highwater FROM {DEFINITION.sources}').fetchone()[0]
    if highwater is None:
        return None
    query = 'SELECT id FROM main.enrichment_conflict WHERE id<=?'
    values = [json.loads(highwater)[0]]
    if cursor:
        query += ' AND id>?'
        values.append(cursor[0])
    row = conn.execute(query+' ORDER BY id LIMIT 1', values).fetchone()
    return row[0] if row else None


def _source(conn, identity):
    columns = [f"CASE WHEN {name} IS NULL THEN NULL ELSE COALESCE(substr(CAST({name} AS BLOB),1,{MAX_BYTES + 1}),X'') END AS {name}"
               if name in TEXT_COLUMNS else name for name in COLUMNS]
    return conn.execute('SELECT '+','.join(columns)+' FROM main.enrichment_conflict WHERE id=?', (identity,)).fetchone()


def _reject_constant(value):
    raise ValueError('Nonfinite JSON constant: ' + value)


def _document(row):
    source = dict(row)
    if sum(len(source[name]) for name in TEXT_COLUMNS if source[name] is not None) > MAX_BYTES:
        raise MigrationError('Metadata conflict exceeds its preparation byte budget')
    for name in TEXT_COLUMNS:
        if source[name] is not None:
            source[name] = bytes(source[name]).decode('utf-8')
    if type(source['resolved']) is not int or source['resolved'] not in STATUSES:
        raise MigrationError('Unsupported legacy conflict status: ' + str(source['id']))
    encoded = json.dumps(source, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(encoded) > MAX_ARCHIVE_BYTES:
        raise MigrationError('Metadata conflict archive exceeds its preparation byte budget')
    try:
        candidates = json.loads(source['candidates'], parse_constant=_reject_constant)
    except (ValueError, TypeError):
        return source, encoded, 'malformed', []
    return (source, encoded, 'list', candidates) if isinstance(candidates, list) else (source, encoded, 'not_list', [])


def _typed(value):
    if value is None:
        return (None,) * len(VALUE_COLUMNS)
    result = asdict(value)
    return tuple(result[name] for name in ('kind', 'text', 'number', 'genre_key', 'unit', 'resolution_reason'))


def describe_candidate(conn, position, raw, candidate, field, entry):
    valid = isinstance(candidate, dict) and isinstance(candidate.get('provider'), str) and bool(candidate['provider']) and 'value' in candidate
    provider = candidate.get('provider') if valid else None
    disposition = ('invalid_candidate' if not valid else 'unsupported_field' if field not in metadata_legacy.CATALOG_FIELDS
                   else 'unproven_subject' if entry is None else 'offered')
    value = metadata_legacy._value(conn, field, candidate['value'], encoded=False) if disposition == 'offered' else None
    return (position, raw, provider, disposition, *_typed(value))


def _candidate(conn, identity, generation, position, raw, candidate, field, entry):
    conn.execute(f'INSERT INTO {CANDIDATES} VALUES ({",".join("?" for _ in range(12))})',
                 (identity, generation, *describe_candidate(conn, position, raw, candidate, field, entry)))


def describe_resolution(conn, source, field, entry):
    if source['resolved'] != 1:
        return
    reason = ('unrecorded_legacy_resolution' if source['resolved_value'] is None else
              'unsupported_field' if field not in metadata_legacy.CATALOG_FIELDS else
              'unproven_subject' if entry is None else None)
    value = metadata_legacy._value(conn, field, source['resolved_value'], encoded=False) if reason is None else None
    if value is not None and value.kind == 'unresolved':
        reason = 'unsupported_legacy_value'
    return (source['resolved_value'], reason, *_typed(value))


def _resolution(conn, identity, generation, source, field, entry):
    value = describe_resolution(conn, source, field, entry)
    if value is not None:
        conn.execute(f'INSERT INTO {RESOLUTIONS} VALUES ({",".join("?" for _ in range(10))})',
                     (identity, generation, *value))


def compatibility(source, body_kind):
    return (*(source[name] for name in ('track_id','field','resolved_value','recording_id','input_video_id','field_scope')),
            source['candidates'] if body_kind != 'list' else None)


class ConflictJob:
    name = DEFINITION.name
    state = DEFINITION.state
    install = staticmethod(DEFINITION.install)
    progress = staticmethod(DEFINITION.progress)

    def step(self, conn, *, batch_size=128, retry=False, clock=time.time):
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError('Preparation batch size must be from 1 through 512')
        if conn.in_transaction:
            raise MigrationError('Preparation requires an idle connection')
        current = None
        try:
            conn.execute('BEGIN')
            DEFINITION._version(conn)
            DEFINITION.validate(conn)
            current = self.progress(conn)
            if current is None:
                raise MigrationError('Preparation is not initialized')
            if current.status == 'failed' and not retry:
                raise MigrationError('Repair the input and retry explicitly: ' + current.error)
            if current.status == 'prepared':
                return current
            identity = _select(conn, current)
            row = _source(conn, identity) if identity is not None else None
            generation = current.input_revision
            cursor = json.loads(current.cursor) if current.cursor else None
            start = cursor[2] if cursor and len(cursor) == 3 and cursor[:2] == [identity, generation] else 0
            existing = conn.execute(f'SELECT 1 FROM {HEAD} WHERE conflict_id=? AND generation=?',
                                    (identity, generation)).fetchone()
            conn.rollback()
            source, encoded, body_kind, candidates = _document(row) if row is not None else (None, None, None, [])
            end = min(start + batch_size, len(candidates))
            batch = [(position, json.dumps(candidates[position], ensure_ascii=False, allow_nan=False), candidates[position])
                     for position in range(start, end)]
            if encoded is not None:
                digest = hashlib.sha256(encoded).hexdigest()
                filename = _publish_encoded(directory(conn), encoded, digest)
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            if source is not None:
                entry, recording, video, field, scope, _, disposition = metadata_legacy._classify(conn, 'enrichment_conflict', source)
                if not existing:
                    conn.execute(f'INSERT INTO {HEAD} VALUES ({",".join("?" for _ in range(15))})',
                        (identity, generation, filename, digest, len(encoded), entry, recording, video, field, scope,
                         disposition, STATUSES[source['resolved']], metadata_legacy._clock(source['updated_at']), body_kind, len(candidates)))
                    conn.execute(f'INSERT INTO {COMPATIBILITY} VALUES (?,?,?,?,?,?,?,?,?)',
                                 (identity, generation, *compatibility(source, body_kind)))
                    _resolution(conn, identity, generation, source, field, entry)
                for position, raw, candidate in batch:
                    _candidate(conn, identity, generation, position, raw, candidate, field, entry)
                if end == len(candidates):
                    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) '
                                 'ON CONFLICT(conflict_id) DO UPDATE SET generation=excluded.generation', (identity, generation))
            elif identity is not None:
                conn.execute(f'DELETE FROM {CURRENT} WHERE conflict_id=?', (identity,))
            owner, checkpoint = current.owner, None
            if identity is not None:
                if end == len(candidates):
                    conn.execute(f'DELETE FROM {DEFINITION.changes} WHERE source_key=json_array(?)', (identity,))
                    checkpoint = json.dumps([identity]) if owner is not None else None
                else:
                    checkpoint = json.dumps([identity, generation, end])
            else:
                owner = None
            complete = owner is None and not conn.execute(f'SELECT 1 FROM {DEFINITION.changes} LIMIT 1').fetchone()
            conn.execute(f'UPDATE {self.state} SET status=?,owner=?,cursor=?,processed=processed+?,error=NULL,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                ('prepared' if complete else 'copying' if owner else 'reconciling', owner, checkpoint,
                 end-start, int(complete), clock()))
            conn.commit()
            return self.progress(conn)
        except Exception as exc:
            conn.rollback()
            if current is not None and not (current.status == 'failed' and not retry):
                conn.execute('BEGIN IMMEDIATE')
                if self.progress(conn) == current:
                    conn.execute(f"UPDATE {self.state} SET status='failed',error=?,prepared_revision=NULL,updated_at=? WHERE id=1",
                                 (str(exc)[:2000], clock()))
                conn.commit()
            raise
        finally:
            conn.rollback()


JOB = ConflictJob()


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
