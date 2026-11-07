"""Captured reporting facts; opaque diagnostics belong to keyed JSON files."""
import hashlib
from contextlib import contextmanager, ExitStack
from . import fao
import json
import math
import os
from pathlib import Path
import tempfile

from . import policy_documents
from .evidence_ledger import canonical

RETAIN_SECONDS = 30 * 86400
MAX_BYTES = 8 * 1024 * 1024
FIELDS = ('policy_kind', 'ranker', 'video_id', 'artist', 'genre', 'ownership', 'last_played',
          'has_last_played', 'has_coverage', 'genre_covered', 'year_covered', 'audio_covered',
          'comparison_id', 'cluster', 'cohort', 'controlled', 'propensity', 'legacy_policy', 'has_experiment')


def definitions():
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
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='served_fact_state'").fetchone())


def active(conn):
    return available(conn) and bool(conn.execute('SELECT active FROM served_fact_state WHERE id=1').fetchone()[0])


def install(conn, context=None):
    if available(conn):
        validate(conn)
        return
    for _, sql in definitions():
        conn.execute(sql)
    # Even an empty installed database starts in compatibility mode. Activation
    # is explicit, never an implicit conversion during ordinary application use.
    conn.execute('INSERT INTO served_fact_state VALUES (1,0,1)')


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
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


def _replaceable(conn, identity, encoded):
    root = directory(conn)
    path = root / relative(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The caller reserves SQLite's writer before allocating or preparing an ID.
    # A failed transaction may leave this ID's orphan; no published owner changes.
    if conn.execute('SELECT 1 FROM served_facts WHERE impression_id=?', (identity,)).fetchone():
        raise ValueError('Served diagnostic is immutable')
    # Failed publications can leave an ID's orphan. A reused legacy ID must
    # also respect readers of its former owner before replacing that file.
    if path.is_file() and fao.read(path).bytes != encoded:
        with (root / '.readers.lock').open('a+b') as lock:
            unlock = policy_documents._try_file_lock(lock, exclusive=True)
            if unlock is None:
                raise ValueError('Served diagnostic identity is still retained by a reader; retry preparation')
            unlock()
    return path


def _capture(conn, identity, trace):
    marks = ','.join('?' for _ in FIELDS)
    conn.execute('INSERT INTO served_facts(impression_id,' + ','.join(FIELDS) + ') VALUES (?,' + marks + ')',
                 (identity, *extract(trace)))
    conn.execute('DELETE FROM served_diagnostic_retirements WHERE impression_id=?', (identity,))


def publish(conn, identity, at, trace):
    encoded = _encoded(identity, at, trace)
    path = _replaceable(conn, identity, encoded)
    fao.write(path, encoded)
    _capture(conn, identity, trace)


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


def expression(conn, field, legacy, alias=''):
    """Installed compatibility reads use old JSON only for not-yet-prepared rows."""
    if not available(conn):
        return legacy
    prefix = alias + '.' if alias else ''
    return (f'CASE WHEN EXISTS(SELECT 1 FROM served_facts f WHERE f.impression_id={prefix}id) '
            f'THEN (SELECT {field} FROM served_facts f WHERE f.impression_id={prefix}id) ELSE {legacy} END')


def _missing(conn, limit):
    return conn.execute('SELECT i.id,i.served_at,i.provenance FROM rec_served_impressions i '
        'LEFT JOIN served_facts f ON f.impression_id=i.id WHERE f.impression_id IS NULL ORDER BY i.id LIMIT ?',
        (limit,)).fetchall()


def _activate(conn):
    conn.execute('UPDATE served_fact_state SET active=1,next_id=max(next_id,'
                 '(SELECT COALESCE(max(id),0)+1 FROM rec_served_impressions)) WHERE id=1')


@contextmanager
def staged_batch(conn, limit=128):
    """Prepare files off-writer; publish only if their captured owners survive.

    A changed or concurrently prepared input returns None, asking the caller
    to retry rather than publishing stale facts or declaring completion.
    """
    if conn.in_transaction:
        raise RuntimeError('Diagnostic staging requires an idle connection')
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('Invalid served preparation batch size')
    rows = [tuple(row) for row in _missing(conn, limit)]
    with ExitStack() as cleanup:
        prepared = []
        for identity, at, provenance in rows:
            trace = json.loads(provenance)
            encoded = _encoded(identity, at, trace)
            path = directory(conn) / relative(identity)
            file = fao.prepare(path, encoded)
            cleanup.callback(file.close)
            prepared.append((identity, encoded, trace, file))

        def publish_prepared():
            if not conn.in_transaction:
                raise RuntimeError('Diagnostic publication requires a writer reservation')
            if [tuple(row) for row in _missing(conn, limit)] != rows:
                return None
            for identity, encoded, trace, file in prepared:
                _replaceable(conn, identity, encoded)
                file.publish()
                _capture(conn, identity, trace)
            if not rows:
                _activate(conn)
            return len(rows)
        yield publish_prepared


def prepare_batch(conn, limit=128):
    """Explicit bounded preparation. Caller commits each batch before continuing."""
    if not 1 <= limit <= 256:
        raise ValueError('Invalid served preparation batch size')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    rows = _missing(conn, limit)
    for row in rows:
        publish(conn, row['id'], row['served_at'], json.loads(row['provenance']))
    if not rows:
        _activate(conn)
    return len(rows)


def compact_provenance(encoded):
    """Keep the policy relationship while facts and diagnostics own the trace."""
    trace = json.loads(encoded)
    thin = {'pool_policy': trace['pool_policy']} if 'pool_policy' in trace else {}
    return json.dumps(thin, separators=(',', ':'))


def insert(conn, rows):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    for row in rows:
        if conn.execute('SELECT 1 FROM rec_served_impressions WHERE request_id=? AND surface=? AND rank=?',
                        (row[0], row[1], row[5])).fetchone():
            continue
        identity = conn.execute('SELECT next_id FROM served_fact_state WHERE id=1').fetchone()[0]
        identity = max(identity, conn.execute('SELECT COALESCE(max(id),0)+1 FROM rec_served_impressions').fetchone()[0])
        trace = json.loads(row[8])
        # Old policy-reference triggers still own these relationships until #626.
        compact = (*row[:8], compact_provenance(row[8]), row[9])
        conn.execute('INSERT INTO rec_served_impressions(id,request_id,surface,lane,mode_id,identity_key,rank,'
                     'score,model_version,provenance,served_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)', (identity, *compact))
        publish(conn, identity, row[9], trace)
        conn.execute('UPDATE served_fact_state SET next_id=? WHERE id=1', (identity + 1,))


def expire(conn, now, limit=256):
    """The caller holds the exclusive file lifetime fence and writer reservation."""
    if not available(conn):
        return 0
    identities = [row[0] for row in conn.execute('SELECT f.impression_id FROM rec_served_impressions i '
        'JOIN served_facts f ON f.impression_id=i.id WHERE i.served_at<? AND f.diagnostic_expired=0 '
        'ORDER BY i.served_at,i.id LIMIT ?', (now - RETAIN_SECONDS, limit))]
    # Commit intentional expiry before unlinking. A crash can leave extra files,
    # but never a published diagnostic whose file maintenance has removed.
    conn.executemany('UPDATE served_facts SET diagnostic_expired=1 WHERE impression_id=?', [(i,) for i in identities])
    conn.executemany('INSERT OR IGNORE INTO served_diagnostic_retirements VALUES (?)', [(i,) for i in identities])
    conn.commit()
    for identity in identities:
        fao.delete(directory(conn) / relative(identity), missing_ok=True)
    return len(identities)


def collect_retired(conn, limit=256):
    """Caller holds the exclusive lifetime fence; the durable queue survives crashes."""
    rows = conn.execute('SELECT impression_id FROM served_diagnostic_retirements ORDER BY impression_id LIMIT ?',
                        (limit,)).fetchall()
    for identity, in rows:
        current = conn.execute('SELECT diagnostic_expired FROM served_facts WHERE impression_id=?', (identity,)).fetchone()
        if current is None or current[0]:
            path = directory(conn) / relative(identity)
            if path.exists() and not fao.delete(path, missing_ok=True):
                continue
        conn.execute('DELETE FROM served_diagnostic_retirements WHERE impression_id=?', (identity,))
    return len(rows)


def collect_orphans(conn, now, limit=256):
    """Bound directory visits as well as deletions; restart repeats one shard.

    Caller owns the domain's exclusive reader fence and SQLite writer reservation,
    so neither a pinned reader nor an uncommitted publisher can lose its file.
    """
    if not conn.in_transaction:
        raise RuntimeError('Orphan collection requires the diagnostic writer reservation')
    root = directory(conn)
    progress = root / '.orphan-collection.json'
    current = getattr(conn, '_served_orphan_gc', None)
    shard = (json.loads(fao.read(progress).bytes)['shard'] if progress.exists() else 0) if current is None else current[0]
    removed = 0
    for _ in range(limit):
        if current is None:
            directory_path = root / str(shard).zfill(2)
            try:
                iterator = os.scandir(directory_path)
            except FileNotFoundError:
                iterator = iter(())
            current = shard, iterator
            conn._served_orphan_gc = current
            if hasattr(iterator, 'close'):
                conn._close_callbacks.append(iterator.close)
        try:
            entry = next(current[1])
        except StopIteration:
            if hasattr(current[1], 'close'):
                current[1].close()
                conn._close_callbacks.remove(current[1].close)
            shard = (shard + 1) % 100
            current = conn._served_orphan_gc = None
            continue
        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith('.json') or not entry.name[:-5].isdecimal():
            continue
        identity = int(entry.name[:-5])
        if identity < 1 or root / relative(identity) != Path(entry.path):
            continue
        if conn.execute('SELECT 1 FROM rec_served_impressions WHERE id=?', (identity,)).fetchone():
            continue
        info = fao.read(entry.path).info
        if info.ctime_ns / 1e9 <= now - RETAIN_SECONDS:
            removed += fao.delete(entry.path, expected=info.identity, missing_ok=True)
    # Persist once per bounded pass. A crash may rescan a shard, never skip it.
    fao.write(progress, canonical({'shard': shard}))
    return removed


def references(conn):
    if available(conn):
        yield from conn.execute('SELECT i.id,i.served_at,f.* FROM rec_served_impressions i '
                                'JOIN served_facts f ON f.impression_id=i.id WHERE f.diagnostic_expired=0')


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
