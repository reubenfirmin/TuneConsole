"""Captured reporting facts; opaque diagnostics belong to keyed JSON files."""
import hashlib
from . import fao
import json
import math
from pathlib import Path
import tempfile

from .evidence_ledger import canonical

RETAIN_SECONDS = 30 * 86400
MAX_BYTES = 8 * 1024 * 1024
FIELDS = ('policy_kind', 'ranker', 'video_id', 'artist', 'genre', 'ownership', 'last_played',
          'has_last_played', 'has_coverage', 'genre_covered', 'year_covered', 'audio_covered',
          'comparison_id', 'cluster', 'cohort', 'controlled', 'propensity', 'legacy_policy', 'has_experiment')


def definitions():
    """The original change 10 schema, used during installed upgrades."""
    yield 'served_fact_state', '''CREATE TABLE served_fact_state (
        id INTEGER PRIMARY KEY CHECK(id=1), active INTEGER NOT NULL CHECK(active IN (0,1)),
        next_id INTEGER NOT NULL CHECK(next_id>0))'''
    yield 'served_facts', '''CREATE TABLE served_facts (
        impression_id INTEGER PRIMARY KEY REFERENCES rec_served_impressions(id) ON DELETE CASCADE,
        policy_kind TEXT NOT NULL, ranker TEXT, video_id TEXT, artist TEXT, genre TEXT, ownership TEXT,
        last_played REAL, has_last_played INTEGER NOT NULL CHECK(has_last_played IN (0,1)),
        has_coverage INTEGER NOT NULL CHECK(has_coverage IN (0,1)),
        genre_covered INTEGER NOT NULL CHECK(genre_covered IN (0,1)),
        year_covered INTEGER NOT NULL CHECK(year_covered IN (0,1)),
        audio_covered INTEGER NOT NULL CHECK(audio_covered IN (0,1)),
        comparison_id TEXT, cluster TEXT, cohort TEXT,
        controlled INTEGER NOT NULL CHECK(controlled IN (0,1)), propensity REAL,
        legacy_policy INTEGER NOT NULL CHECK(legacy_policy IN (0,1)),
        has_experiment INTEGER NOT NULL CHECK(has_experiment IN (0,1)),
        diagnostic_expired INTEGER NOT NULL DEFAULT 0 CHECK(diagnostic_expired IN (0,1)))'''
    yield 'ix_served_fact_video', 'CREATE INDEX ix_served_fact_video ON served_facts(video_id,impression_id) WHERE video_id IS NOT NULL'
    yield 'served_diagnostic_retirements', 'CREATE TABLE served_diagnostic_retirements (impression_id INTEGER PRIMARY KEY)'
    yield 'served_fact_immutable', ('CREATE TRIGGER served_fact_immutable BEFORE UPDATE ON served_facts WHEN '
        + ' OR '.join('NEW.' + field + ' IS NOT OLD.' + field for field in ('impression_id', *FIELDS))
        + " OR NEW.diagnostic_expired<OLD.diagnostic_expired BEGIN SELECT RAISE(ABORT,'Captured served facts are immutable'); END")
    yield 'served_fact_retained', '''CREATE TRIGGER served_fact_retained BEFORE DELETE ON served_facts
        WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id)
        BEGIN SELECT RAISE(ABORT,'Retained impression requires reporting facts'); END'''
    yield 'served_diagnostic_retire', '''CREATE TRIGGER served_diagnostic_retire AFTER DELETE ON served_facts
        BEGIN INSERT OR IGNORE INTO served_diagnostic_retirements VALUES (OLD.impression_id); END'''


def available(conn):
    return any(row[1] == 'policy_kind' for row in conn.execute('PRAGMA main.table_info(rec_served_impressions)'))


def install(conn, context=None):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='served_fact_state'").fetchone():
        validate(conn)
        return
    for _, sql in definitions():
        conn.execute(sql)
    conn.execute('INSERT INTO served_fact_state VALUES (1,0,1)')


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    from .schema_inspection import definitions as read_definitions
    expected = dict(definitions())
    actual = read_definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or _tokens(actual[name]) != _tokens(sql):
            raise MigrationError('Incompatible served fact owner: ' + name)
    if not conn.execute('SELECT 1 FROM served_fact_state WHERE id=1').fetchone():
        raise MigrationError('Missing served fact state')


def root_for_database(path, memory_key):
    return (Path(path).with_name(Path(path).name + '.served') if path else
            Path(tempfile.gettempdir()) / 'tuneconsole-served' / str(memory_key))


def directory(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, getattr(conn, '_policy_memory_key', id(conn)))


def relative(identity):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Invalid served impression identity')
    return Path(str(identity % 100).zfill(2), str(identity) + '.json')


def extract(trace):
    candidate = trace.get('candidate') or {}
    coverage = candidate.get('coverage') or {}
    experiment = trace.get('experiment') or {}
    probability = trace.get('propensity')
    if type(probability) not in (int, float) or not math.isfinite(probability):
        probability = None
    return (trace.get('policy_kind', 'live') if trace.get('policy_kind') is not None else 'live', trace.get('ranker'), candidate.get('video_id'),
            candidate.get('artist'), candidate.get('genre'), candidate.get('ownership'),
            candidate.get('last_played'), int('last_played' in candidate), int('coverage' in candidate),
            int(bool(coverage.get('genre'))), int(bool(coverage.get('year'))), int(bool(coverage.get('audio'))),
            experiment.get('comparison_id'), experiment.get('cluster'), experiment.get('cohort'),
            int(experiment.get('ranker_controlled') is True), probability,
            int((experiment.get('ppr_policy') or {}).get('algorithm') == 'unknown-legacy-cache'), int(bool(experiment)))


def reporting_trace(row):
    """Small compatibility message synthesized from typed facts, never another SQL blob."""
    candidate = {key: row[key] for key in ('video_id', 'artist', 'genre', 'ownership')}
    if row['has_last_played']:
        candidate['last_played'] = row['last_played']
    if row['has_coverage']:
        candidate['coverage'] = {axis: bool(row[axis + '_covered']) for axis in ('genre', 'year', 'audio')}
    experiment = None
    if row['has_experiment']:
        experiment = {key: row[key] for key in ('comparison_id', 'cluster', 'cohort') if row[key] is not None}
        experiment['ranker_controlled'] = bool(row['controlled'])
        if row['legacy_policy']:
            experiment['ppr_policy'] = {'algorithm': 'unknown-legacy-cache'}
    return {'policy_kind': row['policy_kind'], 'ranker': row['ranker'], 'candidate': candidate,
            'experiment': experiment, 'propensity': row['propensity']}


def _encoded(identity, at, trace):
    body = {'format': 1, 'impression_id': identity, 'served_at': at, 'provenance': trace}
    encoded = canonical({'body': body, 'sha256': hashlib.sha256(canonical(body)).hexdigest()})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Served diagnostic exceeds its byte budget')
    return encoded


def diagnostic(conn, row):
    if row['diagnostic_expired']:
        return None
    path = directory(conn) / relative(row['id'])
    try:
        with fao.read(path) as stream:
            encoded = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        raise ValueError('Unexpectedly missing served diagnostic: ' + str(row['id'])) from None
    if len(encoded) > MAX_BYTES:
        raise ValueError('Served diagnostic exceeds its byte budget')
    return _verified_diagnostic(encoded, row)


def _verified_diagnostic(encoded, row):
    document = json.loads(encoded)
    body = document.get('body') if isinstance(document, dict) else None
    if (not isinstance(body, dict) or body.get('format') != 1
            or body.get('impression_id') != row['id'] or body.get('served_at') != row['served_at']
            or hashlib.sha256(canonical(body)).hexdigest() != document.get('sha256')
            or extract(body['provenance']) != tuple(row[field] for field in FIELDS)):
        raise ValueError('Served diagnostic differs from its captured owner')
    return body['provenance']


def expression(field, legacy, alias=''):
    """Read the original inline trace when the impression has no diagnostic file."""
    prefix = alias + '.' if alias else ''
    return f'CASE WHEN {prefix}policy_kind IS NOT NULL THEN {prefix}{field} ELSE {legacy} END'


def captured(row):
    if row['policy_kind'] is not None:
        return {key: row[key] for key in (*FIELDS, 'diagnostic_expired')}


def compact_provenance(encoded):
    """Keep the policy relationship while facts and diagnostics own the trace."""
    trace = json.loads(encoded)
    thin = {'pool_policy': trace['pool_policy']} if 'pool_policy' in trace else {}
    return json.dumps(thin, separators=(',', ':'))


def references(conn):
    if available(conn):
        yield from conn.execute('SELECT id,served_at,' + ','.join(FIELDS) + ',diagnostic_expired '
                                'FROM rec_served_impressions WHERE policy_kind IS NOT NULL AND diagnostic_expired=0')


def export(conn, target):
    """Caller holds the reader fence and the backup's pinned SQL snapshot."""
    import shutil
    target = Path(target)
    entries = []
    for row in references(conn):
        diagnostic(conn, row)
        path = relative(row['id'])
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        fao.write(destination, fao.read(directory(conn) / path, max_bytes=MAX_BYTES))
        encoded = fao.read(destination, max_bytes=MAX_BYTES).bytes
        entries.append([row['id'], len(encoded), hashlib.sha256(encoded).hexdigest()])
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / 'manifest.json'
    fao.write(manifest, canonical({'format': 'served-diagnostics-1', 'entries': entries}))
    return str(manifest)


def restore(conn, manifest):
    manifest = Path(manifest)
    value = json.loads(fao.read(manifest).bytes)
    if not isinstance(value, dict) or set(value) != {'format', 'entries'} or value['format'] != 'served-diagnostics-1':
        raise ValueError('Unsupported served diagnostics backup')
    expected = {row['id']: row for row in references(conn)}
    # Stage a verified copy of every member first. A bad later member (or an
    # incomplete manifest) must not replace any previously usable live trace.
    with tempfile.TemporaryDirectory(prefix='tuneconsole-served-restore-') as staging:
        staged = Path(staging)
        seen = set()
        for identity, size, digest in value['entries']:
            if identity not in expected or identity in seen:
                raise ValueError('Served diagnostics backup differs from SQL ownership')
            seen.add(identity)
            source = manifest.parent / relative(identity)
            prepared = staged / relative(identity)
            fao.write(prepared, fao.read(source, max_bytes=MAX_BYTES))
            encoded = fao.read(prepared, max_bytes=MAX_BYTES).bytes
            if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
                raise ValueError('Served diagnostic backup failed verification')
            _verified_diagnostic(encoded, expected[identity])
        if seen != expected.keys():
            raise ValueError('Served diagnostics backup is incomplete')
        for identity in seen:
            fao.write(directory(conn) / relative(identity), fao.read(staged / relative(identity), max_bytes=MAX_BYTES))
