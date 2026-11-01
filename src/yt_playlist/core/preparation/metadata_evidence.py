"""Prepare retained v79 metadata without activating or retiring evidence owners.

Original rows remain authoritative. Typed staging preserves null, blank,
unresolved and absent evidence separately, and never infers historical subject
identity from today's catalog entry. Publication and source retirement require
a later verified boundary; this job does not stamp v80 complete.
"""
from dataclasses import asdict
import hashlib
import json

from .. import metadata_legacy, metadata_values
from ..migrations.runner import MigrationError
from .rows import Job, main as run


KEYS = {
    'metadata_findings': ('id',),
    'enrichment_log': ('id',),
    'metadata_decisions': ('id',),
    'entry_field_choices': ('entry_id', 'field'),
    'provider_attempts': ('id',),
    'enrichment_provider_state': ('recording_id', 'provider'),
    'recording_identifiers': ('id',),
    'catalog_field_hints': ('entry_id', 'field', 'source'),
    'recording_field_sources': ('recording_id', 'field'),
    'item_field_sources': ('entry_id', 'field'),
}
TABLE = 'prepared_metadata_evidence'
MAX_OPERATIONAL_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
SOURCE_COLUMNS = {
    'metadata_findings': ('id','entry_id','recording_id','field','provider','value_json','observed_at','provenance_json','observation_key'),
    'enrichment_log': ('id','track_id','run_id','provider','field','value','created_at','recording_id','input_video_id','field_scope'),
    'metadata_decisions': ('id','entry_id','field','value_json','source','chosen_at','legacy_ref'),
    'entry_field_choices': ('entry_id','field','value_json','source'),
    'provider_attempts': ('id','recording_id','input_video_id','provider','at','status','fields_json'),
    'enrichment_provider_state': ('recording_id','provider','cycle','at','status','fields_json','failures','retry_at','error'),
    'recording_identifiers': ('id','recording_id','scheme','entity_type','value','source','input_video_id','status','observed_at'),
    'catalog_field_hints': ('entry_id','field','source','value_json','active'),
    'recording_field_sources': ('recording_id','field','source','selected_at'),
    'item_field_sources': ('entry_id','field','source','selected_at'),
}
NUMBER_COLUMNS = {'id','entry_id','track_id','recording_id','observed_at','created_at','chosen_at',
                  'at','cycle','failures','retry_at','active','selected_at'}
ORIGINALS = 'prepared_metadata_originals'
ORIGINAL_SQL = f'''CREATE TABLE {ORIGINALS} (
    owner TEXT NOT NULL, source_key TEXT NOT NULL CHECK(json_valid(source_key)),
    digest TEXT NOT NULL CHECK(length(digest)=64), source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
    PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''
SQL = f'''CREATE TABLE {TABLE} (
    owner TEXT NOT NULL, source_key TEXT NOT NULL CHECK(json_valid(source_key)),
    source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
    source_digest TEXT NOT NULL,
    entry_id INTEGER, recording_id INTEGER, input_video_id TEXT, field TEXT, field_scope TEXT,
    disposition TEXT NOT NULL CHECK(disposition IN ('catalog_evidence','native_observation',
        'unsupported_field','invalid_value','unproven_subject','unproven_scope','operational_state','selection')),
    observed_at REAL, provider TEXT, native_provenance TEXT CHECK(native_provenance IS NULL OR json_valid(native_provenance)),
    value_kind TEXT CHECK(value_kind IN ('value','null','blank','unresolved','withdrawn')),
    text_value TEXT, number_value, genre_key TEXT, unit TEXT, resolution_reason TEXT,
    PRIMARY KEY(owner,source_key),
    FOREIGN KEY(owner,source_key,source_digest) REFERENCES {ORIGINALS}(owner,source_key,digest),
    CHECK(number_value IS NULL OR (typeof(number_value) IN ('integer','real') AND
          number_value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)),
    CHECK(value_kind IS NOT NULL OR (text_value IS NULL AND number_value IS NULL AND genre_key IS NULL
          AND unit IS NULL AND resolution_reason IS NULL))
) WITHOUT ROWID'''
OPERATIONAL = 'prepared_metadata_operational_fields'
OPERATIONAL_SQL = f'''CREATE TABLE {OPERATIONAL} (
    owner TEXT NOT NULL CHECK(owner IN ('provider_attempts','enrichment_provider_state')),
    source_key TEXT NOT NULL, source_digest TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position>=0), field TEXT NOT NULL, canonical_field TEXT NOT NULL,
    recording_id INTEGER, entry_id INTEGER CHECK(entry_id IS NULL), input_video_id TEXT,
    provider TEXT NOT NULL, raw_value TEXT NOT NULL CHECK(json_valid(raw_value)),
    value_kind TEXT CHECK(value_kind IN ('value','null','blank','unresolved','withdrawn')),
    text_value TEXT, number_value, genre_key TEXT, unit TEXT, resolution_reason TEXT,
    PRIMARY KEY(owner,source_key,source_digest,field),
    FOREIGN KEY(owner,source_key,source_digest) REFERENCES {ORIGINALS}(owner,source_key,digest),
    CHECK(number_value IS NULL OR (typeof(number_value) IN ('integer','real') AND
          number_value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308))) WITHOUT ROWID'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def describe(conn, owner, key, source):
    source = dict(source)
    total = 0
    for name, value in source.items():
        if name in NUMBER_COLUMNS or value is None:
            continue
        raw = value.encode('utf-8') if isinstance(value, str) else bytes(value)
        if name == 'fields_json' and len(raw) > MAX_OPERATIONAL_BYTES:
            raise MigrationError('Provider fields exceed their preparation byte budget')
        total += len(raw)
        if total > MAX_SOURCE_BYTES:
            raise MigrationError('Metadata source exceeds its preparation byte budget')
        source[name] = raw.decode('utf-8')
    if owner in ('recording_field_sources', 'item_field_sources'):
        field = source['field']
        scope = metadata_values.scope(field)
        entry, recording = source.get('entry_id'), source.get('recording_id')
        allowed = metadata_values.RECORDING_FIELDS if recording is not None else metadata_values.ITEM_FIELDS
        if field not in allowed:
            raise MigrationError('Unsupported metadata selection: ' + owner + '.' + field)
        video, native, disposition = None, None, 'selection'
    else:
        entry, recording, video, field, scope, native, disposition = metadata_legacy._classify(conn, owner, source)
    prepared = None
    if (owner in (*metadata_legacy.OBSERVATION_OWNERS, 'catalog_field_hints')
            and field in metadata_legacy.CATALOG_FIELDS and (native is None or native['scope'] == 'recording')):
        raw = source.get('value') if owner == 'enrichment_log' else source.get('value_json')
        prepared = metadata_legacy._value(conn, field, raw, encoded=owner != 'enrichment_log',
                                         unit=native.get('unit') if native else None)
        if owner == 'entry_field_choices' and prepared.kind == 'unresolved':
            raise MigrationError('Unsupported active metadata choice: ' + key)
    values = asdict(prepared) if prepared is not None else {}
    snapshot = encoded(source)
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    parameters = (owner, key, snapshot, digest, entry, recording, video, field, scope, disposition,
                  metadata_legacy._observed_at(source), source.get('provider', source.get('source')),
                  encoded(native) if native is not None else None, values.get('kind'), values.get('text'),
                  values.get('number'), values.get('genre_key'), values.get('unit'), values.get('resolution_reason'))
    operational = list(describe_operational(conn, owner, key, source, digest, recording, video)) if owner in (
        'provider_attempts', 'enrichment_provider_state') else []
    return parameters, operational


def copy(conn, owner, key, source):
    parameters, operational = describe(conn, owner, key, source)
    snapshot, digest = parameters[2:4]
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?) ON CONFLICT DO NOTHING', (owner, key, digest, snapshot))
    columns = ('owner', 'source_key', 'source_snapshot', 'source_digest', 'entry_id', 'recording_id', 'input_video_id',
               'field', 'field_scope', 'disposition', 'observed_at', 'provider', 'native_provenance',
               'value_kind', 'text_value', 'number_value', 'genre_key', 'unit', 'resolution_reason')
    updates = ','.join(c+'=excluded.'+c for c in columns[2:])
    conn.execute(f'INSERT INTO {TABLE}(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) +
                 ') ON CONFLICT(owner,source_key) DO UPDATE SET ' + updates, parameters)
    for values in operational:
        conn.execute(f'INSERT INTO {OPERATIONAL} VALUES ({",".join("?" for _ in values)}) '
            'ON CONFLICT(owner,source_key,source_digest,field) DO UPDATE SET '
            'recording_id=excluded.recording_id,input_video_id=excluded.input_video_id,'
            'value_kind=excluded.value_kind,text_value=excluded.text_value,number_value=excluded.number_value,'
            'genre_key=excluded.genre_key,unit=excluded.unit,resolution_reason=excluded.resolution_reason', values)


def describe_operational(conn, owner, key, source, digest, recording, video):
    fields = json.loads(source['fields_json'])
    if not isinstance(fields, dict) or fields.keys() - set(metadata_legacy.ATTEMPT_FIELDS):
        raise MigrationError('Provider fields require the finite supported field map')
    if recording is None:
        raise MigrationError('Provider fields require their original recorded subject')
    for position, (field, raw) in enumerate(fields.items()):
        canonical = 'mb_year' if field == 'year' else field
        prepared = metadata_legacy._value(conn, canonical, raw, encoded=False) if canonical in metadata_legacy.CATALOG_FIELDS else None
        values = asdict(prepared) if prepared is not None else {}
        parameters = (owner, key, digest, position, field, canonical, recording, None, video,
                      source['provider'], encoded(raw), values.get('kind'), values.get('text'),
                      values.get('number'), values.get('genre_key'), values.get('unit'), values.get('resolution_reason'))
        yield parameters


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {TABLE} WHERE owner=? AND source_key=?', (owner, key))


def _projection(columns):
    return tuple(column if column in NUMBER_COLUMNS else
        f'CASE WHEN {column} IS NULL THEN NULL ELSE COALESCE(substr(CAST({column} AS BLOB),1,'
        f'{(MAX_OPERATIONAL_BYTES if column == "fields_json" else MAX_SOURCE_BYTES)+1}),X\'\') END AS {column}' for column in columns)


JOB = Job('metadata', 79, 79, KEYS, ((ORIGINALS, ORIGINAL_SQL), (TABLE, SQL), (OPERATIONAL, OPERATIONAL_SQL)), copy, delete,
          dependencies=('catalog_entries', 'recordings', 'youtube_items', 'genre_catalog', 'genre_alias'),
          projections={owner:_projection(columns) for owner, columns in SOURCE_COLUMNS.items()}, batch_limit=16)


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
