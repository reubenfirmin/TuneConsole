"""Prepare retained taste controls without replacing their installed settings.

Exact originals, native recovery files and a paged member plan are durable before
each bounded SQL batch. Catalog references are observations, never admissions.
"""
import hashlib
import json
from pathlib import Path

from .. import catalog_keys, collection_recovery, policy_documents, recommendation_outputs as files
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from ..recommendation_work import finite
from ..taste_control_state import BASELINE, CHECKPOINT
from . import document_plan, taste_axes
from .ordered_rows import OrderedJob
from .rows import Job, main as run


DECISIONS = 'ppr_experiment_decisions'
SOURCES = {'checkpoint': CHECKPOINT, 'baseline': BASELINE, 'decisions': DECISIONS}
PREFIXES = {CHECKPOINT: 'prepared_taste_checkpoint', BASELINE: 'prepared_taste_comparison',
            DECISIONS: 'prepared_experiment_decisions'}
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_BYTES = MAX_SOURCE_BYTES
PART_CHARACTERS = 64 * 1024
NUMBER = "typeof({field}) IN ('integer','real') AND abs({field})<=1.7976931348623157e308"


def numeric(field, nullable=False):
    condition = NUMBER.format(field=field)
    return f'{field} REAL ' + (f'CHECK({field} IS NULL OR ({condition}))' if nullable else f'NOT NULL CHECK({condition})')


def table(source, suffix):
    return PREFIXES[source] + '_' + suffix


def tables(source):
    return tuple(table(source, suffix) for suffix in ('generations', 'parts', 'heads'))


def definitions(source):
    head = table(source, 'generations')
    recovery = '' if source == DECISIONS else f''',
        recovery_filename TEXT, recovery_digest TEXT CHECK(length(recovery_digest)=64),
        recovery_bytes INTEGER CHECK(recovery_bytes BETWEEN 1 AND {collection_recovery.MAX_BYTES}),
        CHECK((recovery_filename IS NULL)=(recovery_digest IS NULL)),
        CHECK((recovery_filename IS NULL)=(recovery_bytes IS NULL)),
        CHECK((state_present=0)=(recovery_filename IS NULL))'''
    saved = ', ' + numeric('saved_at', nullable=True) if source == CHECKPOINT else ''
    yield head, f'''CREATE TABLE {head} (
        source TEXT NOT NULL CHECK(source={source!r}), preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        state_present INTEGER NOT NULL CHECK(state_present IN (0,1)),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_SOURCE_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512), member_count INTEGER NOT NULL CHECK(member_count>=0),
        unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        plan_filename TEXT NOT NULL, plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
        plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}){saved}{recovery},
        PRIMARY KEY(source,preparation_revision)) WITHOUT ROWID'''
    current = table(source, 'heads')
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY CHECK(source={source!r}), preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    specs = {'parts': ('''position INTEGER NOT NULL CHECK(position>=0), filename TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576)''', '')}
    if source == CHECKPOINT:
        specs['weights'] = (f'''position INTEGER NOT NULL CHECK(position>=0), captured_axis TEXT NOT NULL,
            axis_digest TEXT NOT NULL CHECK(length(axis_digest)=64), axis_ref INTEGER CHECK(axis_ref>0),
            kind TEXT NOT NULL CHECK(kind IN ('genre','family','other','unresolved_genre')),
            genre_key TEXT, family_key TEXT, value TEXT, resolution_reason TEXT,
            {numeric('weight')}, {numeric('updated_at', nullable=True)},
            CHECK((kind='genre' AND genre_key IS NOT NULL AND family_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
               OR (kind='family' AND family_key IS NOT NULL AND genre_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
               OR (kind='other' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND resolution_reason IS NULL)
               OR (kind='unresolved_genre' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL
                   AND resolution_reason IN ('unknown_genre','ambiguous_genre')))''',
            ', UNIQUE(source,preparation_revision,axis_digest)')
    elif source == BASELINE:
        specs['entries'] = (f'''position INTEGER NOT NULL CHECK(position>=0), recording_id INTEGER CHECK(recording_id>0),
            unresolved_key TEXT, captured_key TEXT NOT NULL CHECK(length(captured_key)>0),
            rank INTEGER CHECK(rank IS NULL OR (typeof(rank)='integer' AND rank>0)), {numeric('score')}, lane TEXT NOT NULL,
            {numeric('lane_weight', nullable=True)},
            CHECK((recording_id IS NOT NULL AND unresolved_key IS NULL) OR
                (recording_id IS NULL AND unresolved_key IS NOT NULL AND length(unresolved_key)>0))''',
            ', UNIQUE(source,preparation_revision,recording_id), UNIQUE(source,preparation_revision,unresolved_key)')
    else:
        specs['decisions'] = (f'''position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 199),
            decision TEXT NOT NULL CHECK(decision IN ('promote','retire')), cohort TEXT NOT NULL CHECK(length(cohort)<=100),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000), {numeric('created_at')},
            powered INTEGER NOT NULL CHECK(powered IN (0,1)), basis TEXT NOT NULL,
            recovery_filename TEXT NOT NULL, recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
            recovery_bytes INTEGER NOT NULL CHECK(recovery_bytes BETWEEN 1 AND {collection_recovery.MAX_BYTES})''', '')
    for suffix, (columns, unique) in specs.items():
        name = table(source, suffix)
        yield name, f'''CREATE TABLE {name} (
            source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, {columns},
            PRIMARY KEY(source,preparation_revision,position),
            FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision){unique}) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Taste control preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'taste-controls'


def recovery(root, kind, owner, value):
    encoded = canonical({'format': collection_recovery.FORMAT, 'kind': kind, 'owner': str(owner), 'state': value})
    if len(encoded) > collection_recovery.MAX_BYTES:
        raise ValueError('Taste control recovery exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    return policy_documents._publish_encoded(root, encoded, digest), digest, len(encoded)


def nullable_number(value):
    return finite(value) if value is not None else None


def checkpoint(conn, root, value, fields, *, retain=recovery):
    if (not isinstance(value, dict) or set(value) != {'saved_at', 'params', 'weights'}
            or not isinstance(value['params'], dict) or not isinstance(value['weights'], list)
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value['params'].items())):
        raise ValueError('Unsupported taste settings checkpoint')
    fields['saved_at'] = finite(value['saved_at'])
    fields['member_count'] = len(value['weights'])
    reference = retain(root, 'taste-checkpoint', 1, value['params'])
    fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), reference))
    seen = set()
    has_axes = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone()
    for position, weight in enumerate(value['weights']):
        if not isinstance(weight, dict) or set(weight) != {'axis', 'weight', 'updated_at'}:
            raise ValueError('Unsupported checkpoint weight')
        digest, interpretation = taste_axes.axis(conn, weight['axis'])
        if digest in seen:
            raise ValueError('Checkpoint has competing weights for the same captured axis')
        seen.add(digest)
        kind, genre, family, token, reason = interpretation
        axis = conn.execute('SELECT id FROM taste_axes WHERE kind=? AND genre_key IS ? AND family_key IS ? AND value IS ?',
                            (kind, genre, family, token)).fetchone() if has_axes else None
        yield 'weight', (position, weight['axis'], digest, axis[0] if axis else None, *interpretation,
                         finite(weight['weight']), nullable_number(weight['updated_at']))


def baseline(conn, root, value, fields, *, retain=recovery):
    if not isinstance(value, list):
        raise ValueError('Taste comparison baseline must be an ordered list')
    fields['member_count'] = len(value)
    traces, seen = [], set()
    for position, item in enumerate(value):
        if (not isinstance(item, dict) or set(item) != {'key', 'rank', 'score', 'trace', 'lane', 'lane_weight'}
                or not isinstance(item['key'], str) or not item['key'] or not isinstance(item['lane'], str)
                or (item['rank'] is not None and (type(item['rank']) is not int or item['rank'] <= 0))):
            raise ValueError('Unsupported taste comparison entry')
        resolved = catalog_keys.resolve_key(conn, item['key'])
        recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (resolved,)).fetchone() if resolved else None
        identity = ('recording', recording[0]) if recording else ('unresolved', item['key'])
        if identity in seen:
            raise ValueError('Comparison has repeated captured recording identities')
        seen.add(identity)
        traces.append(item['trace'])
        yield 'entry', (position, recording[0] if recording else None, None if recording else item['key'], item['key'],
                        item['rank'], finite(item['score']), item['lane'], nullable_number(item['lane_weight']))
    reference = retain(root, 'taste-comparison', 1, traces)
    fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), reference))


def decisions(root, value, fields, *, retain=recovery):
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError('Experiment decisions require at most 200 ordered records')
    fields['member_count'] = len(value)
    for position, item in enumerate(value):
        if (not isinstance(item, dict) or set(item) != {'decision', 'cohort', 'reason', 'at', 'powered', 'basis', 'live_evidence'}
                or item['decision'] not in ('promote', 'retire') or not isinstance(item['cohort'], str) or len(item['cohort']) > 100
                or not isinstance(item['reason'], str) or not 1 <= len(item['reason'].strip(' ')) <= 2000
                or type(item['powered']) is not bool or not isinstance(item['basis'], str)
                or not isinstance(item['live_evidence'], dict) or set(item['live_evidence']) != {'ppr', 'cosine'}):
            raise ValueError('Unsupported experiment decision')
        reference = retain(root, 'experiment-decision', position + 1, item['live_evidence'])
        yield 'decision', (position, item['decision'], item['cohort'], item['reason'], finite(item['at']),
                           int(item['powered']), item['basis'], *reference)


def describe(source, text):
    raw = text.encode('utf-8') if text is not None else b''
    if len(raw) > MAX_SOURCE_BYTES:
        raise MigrationError('Taste control exceeds its preparation byte budget')
    value = json.loads(text) if text else ([] if source == DECISIONS else None)
    fields = {'source_kind': 'null' if text is None else 'blank' if text == '' else 'recorded',
              'state_present': int(value is not None), 'raw_digest': hashlib.sha256(raw).hexdigest(),
              'raw_size': len(raw), 'part_count': max(1, (len(text or '') + PART_CHARACTERS - 1) // PART_CHARACTERS),
              'member_count': 0}
    if source == CHECKPOINT:
        fields['saved_at'] = None
    if source != DECISIONS:
        fields.update(recovery_filename=None, recovery_digest=None, recovery_bytes=None)
    return fields, value


def prepare(conn, row, *, revision, offset, rebuild=False):
    source = row['key']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(source,"generations")} '
                            'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared taste checkpoint has no durable plan')
        return document_plan.load(source, previous)
    text = bytes(row['value']).decode('utf-8') if row['value'] is not None else None
    fields, value = describe(source, text)
    root = directory(conn)
    def units():
        for position in range(fields['part_count']):
            part = None if text is None else text[position * PART_CHARACTERS:(position + 1) * PART_CHARACTERS]
            yield 'part', (position, *files.publish(root, files.encode(source, 'original:' + str(position), part)))
        if source == DECISIONS:
            yield from decisions(root, value, fields)
        elif value is not None:
            yield from checkpoint(conn, root, value, fields) if source == CHECKPOINT else baseline(conn, root, value, fields)
    result = document_plan.build(root, source, fields, units())
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt taste plan differs from its captured checkpoint')
    return result


def begin(conn, row, revision, plan):
    values = {'source': row['key'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table(row["key"],"generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})',
                 tuple(values.values()))


def copy(conn, row, revision, position, unit):
    kind, values = unit
    suffix = {'part': 'parts', 'weight': 'weights', 'entry': 'entries', 'decision': 'decisions'}[kind]
    conn.execute(f'INSERT INTO {table(row["key"],suffix)} VALUES ({",".join("?" for _ in range(2+len(values)))})',
                 (row['key'], revision, *values))


def complete(conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table(row["key"],"heads")} VALUES (?,?) '
                 'ON CONFLICT(source) DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['key'], revision))


def delete(conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table(source,"heads")} WHERE source=?', (source,))


def resume_read(conn, owner, key):
    source, = json.loads(key)
    return conn.execute('SELECT key FROM main.settings WHERE key=? AND json_array(key)=?', (source, key)).fetchone()


def head(conn, source):
    return conn.execute(f'SELECT g.* FROM {table(source,"heads")} h JOIN {table(source,"generations")} g '
                        'USING(source,preparation_revision) WHERE source=?', (source,)).fetchone()


def read_original(conn, generation):
    source = generation['source']
    parts = list(conn.execute(f'SELECT * FROM {table(source,"parts")} WHERE source=? AND preparation_revision=? ORDER BY position LIMIT 513',
                             (source, generation['preparation_revision'])))
    if len(parts) != generation['part_count'] or [row['position'] for row in parts] != list(range(len(parts))):
        raise MigrationError('Prepared taste original has incomplete parts')
    values = [files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
                         chunk_key='original:' + str(row['position'])) for row in parts]
    value = None if generation['source_kind'] == 'null' else ''.join(values)
    if value is None and values != [None]:
        raise MigrationError('Prepared taste original has an invalid null source')
    raw = value.encode() if value is not None else b''
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared taste original differs from its captured bytes')
    return value


def definition(name, source):
    dependencies, optional = (), ()
    if source == CHECKPOINT:
        dependencies = ('genre_catalog', 'genre_alias', 'genre_family')
        optional = ('genre_redirects', 'taste_axes')
    elif source == BASELINE:
        dependencies = ('recordings', 'recording_aliases', 'recording_redirects')
    return Job('taste_' + name, 79, 95, {'settings': ('key',)}, tuple(definitions(source)), None, None,
        dependencies=dependencies, optional_dependencies=optional, filters={'settings': '{prefix}key=' + repr(source)},
        projections={'settings': ('key', f'CASE WHEN value IS NULL THEN NULL ELSE '
                     f"COALESCE(substr(CAST(value AS BLOB),1,{MAX_SOURCE_BYTES + 1}),X'') END AS value")})


def repair_files(conn, progress, *, source):
    """Recreate exact missing files without rewriting retained generations."""
    try:
        conn.execute('BEGIN')
        captured = head(conn, source)
        if captured is None or captured['preparation_revision'] != progress.input_revision:
            return
        row = conn.execute('SELECT key,CASE WHEN value IS NULL THEN NULL ELSE '
            "COALESCE(substr(CAST(value AS BLOB),1,?),X'') END AS value FROM main.settings WHERE key=?",
            (MAX_BYTES+1, source)).fetchone()
        raw = bytes(row['value']) if row is not None and row['value'] is not None else b''
        if row is None or hashlib.sha256(raw).hexdigest() != captured['raw_digest']:
            return
    finally:
        conn.rollback()
    plan = prepare(conn, row, revision=progress.input_revision, offset=0)
    if plan.manifest != tuple(captured[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt taste plan differs from its retained generation')


def worker(name, source):
    from functools import partial
    return OrderedJob(definition(name, source), prepare, begin, copy, complete, delete,
        revision_aware=True, resume_read=resume_read, repair_files=partial(repair_files, source=source))


JOBS = {name: worker(name, source) for name, source in SOURCES.items()}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
