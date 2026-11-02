"""Native descriptive evidence stays in its supplying metadata delivery.

The finite catalog vocabulary remains separate. Musical source rows are current
dependency indexes; their selected field references do not copy native values or
matching documents into a second historical payload store.
"""
from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import json

from . import metadata_deliveries as deliveries
from .activity import WorkBudgetExceeded
from .db_revisions import keyed_reads
from .evidence_ledger import canonical

FORMAT = 'native-metadata-observations-1'
FIELDS = frozenset(('tags', 'bpm', 'energy', 'danceability', 'mood_party', 'mood_aggressive',
                    'music_key', 'music_scale', 'mood_happy', 'mood_sad', 'mood_relaxed',
                    'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity'))
MAX_OBSERVATIONS = 64
RUNTIME_HEAD_SQL = '''CREATE TABLE metadata_native_delivery_heads (
    source_key TEXT PRIMARY KEY NOT NULL REFERENCES rec_musical_sources(source_key) ON DELETE CASCADE,
    run_id TEXT NOT NULL,accepted_at REAL NOT NULL,
    result_hash BLOB CHECK(result_hash IS NULL OR (typeof(result_hash)='blob' AND length(result_hash)=32))) WITHOUT ROWID'''


def install_runtime_heads(conn, context=None):
    """Bounded DDL only; installed freshness is adopted during explicit preparation or admission."""
    conn.execute(RUNTIME_HEAD_SQL)


def validate_runtime_heads(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    row = conn.execute("SELECT sql FROM sqlite_schema WHERE name='metadata_native_delivery_heads'").fetchone()
    if row is None or _tokens(row[0]) != _tokens(RUNTIME_HEAD_SQL):
        raise MigrationError('Incompatible native metadata delivery heads')


def _result_hash(values):
    # This source owns normalized model evidence. Input-title matching details
    # can differ between recordings sharing an artist/release in the same run.
    normalized = {payload['field']: _profile_value(payload) for payload in values}
    return hashlib.sha256(canonical(normalized)).digest()


def installed_runtime_head(conn, key, previous):
    """Use known capture provenance without inventing it for older converted sources."""
    if previous.get('run_id') is not None and previous.get('at') is not None:
        raw = previous.get('raw')
        return {'run_id': previous['run_id'], 'accepted_at': previous['at'],
                'result_hash': _result_hash(raw) if isinstance(raw, list) else None}
    if not available(conn):
        return None
    # At most the finite selected fields are inspected. This also retains a
    # newer identical legacy delivery, whose selection advanced without a new
    # model-input revision. No historical graph is rebuilt on ordinary startup.
    row = conn.execute('SELECT d.id,d.delivery_key,d.observed_at FROM musical_source_observations s '
        'JOIN native_metadata_observations o ON o.id=s.observation_id '
        'JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE s.source_key=? '
        'ORDER BY d.observed_at DESC,d.id DESC LIMIT 1', (key,)).fetchone()
    if row is None or row['observed_at'] is None:
        return None
    body = deliveries.response(conn, row['id'])
    raw = body.get('observations') if isinstance(body, dict) and body.get('format') == FORMAT else None
    return {'run_id': row['delivery_key'], 'accepted_at': row['observed_at'],
            'result_hash': _result_hash(raw) if isinstance(raw, list) else None}


def retain_runtime_head(conn, key, head):
    conn.execute('INSERT INTO metadata_native_delivery_heads VALUES (?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET '
        'run_id=excluded.run_id,accepted_at=excluded.accepted_at,result_hash=excluded.result_hash',
        (key, head['run_id'], head['accepted_at'], head['result_hash']))


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='native_metadata_observations'").fetchone() is not None


def _profile_value(payload):
    provenance = {key: value for key, value in payload.items() if key not in ('field', 'value')}
    provenance.pop('delivery_id', None)
    match = dict(provenance.get('match_evidence') or {})
    for key in ('input_title', 'input_artist', 'returned_title', 'returned_artist', 'recording_mbid', 'search_score'):
        match.pop(key, None)
    if 'match_evidence' in provenance:
        provenance['match_evidence'] = match
    return {'value': payload['value'], 'provenance': provenance}


def read(conn, observation_id):
    row = conn.execute('SELECT o.*,d.key_contract,d.raw_byte_size FROM native_metadata_observations o '
                       'JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?', (observation_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown native metadata observation')
    return _payload(row, deliveries.response(conn, row['delivery_id']))


def _payload(row, body):
    if row['legacy_finding_id'] is not None:
        if (not isinstance(body, dict) or body.get('format') != 'legacy-metadata-record-1'
                or body.get('owner') != 'metadata_findings'
                or body.get('row', {}).get('id') != row['legacy_finding_id']):
            raise ValueError('Native metadata original has a different owner')
        original = body['row']
        payload = {**json.loads(original['provenance_json']), 'field': original['field'],
                   'value': json.loads(original['value_json'])}
    else:
        if (not isinstance(body, dict) or body.get('format') != FORMAT
                or not isinstance(body.get('observations'), list)
                or not 0 <= row['position'] < len(body['observations'])):
            raise ValueError('Invalid native metadata delivery')
        payload = body['observations'][row['position']]
    if (not isinstance(payload, dict) or payload.get('schema') != 1 or payload.get('field') != row['field']
            or payload.get('kind') != row['kind'] or payload.get('unit') != row['unit']):
        raise ValueError('Native metadata reference differs from its delivery')
    return payload


def source_values(conn, source_key, max_bytes=256 * 1024, *, store=None):
    from . import metadata_runtime as runtime, metadata_runtime_files as files
    if runtime.active(conn):
        row = conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', (source_key,)).fetchone()
        if row is None:
            return {}
        return files.read(conn, 'native', (source_key,), row[0], max_bytes=max_bytes)['observations']
    return installed_source_values(conn, source_key, max_bytes, store=store)


def installed_source_values(conn, source_key, max_bytes=256 * 1024, *, store=None):
    """Read captured SQL evidence, including while verifying its replacement."""
    with ExitStack() as guards:
        if store is not None:
            guards.enter_context(keyed_reads(store, 'musical_source_observations', [source_key]))
            observations = [row[0] for row in conn.execute(
                'SELECT observation_id FROM musical_source_observations WHERE source_key=?', (source_key,))]
            guards.enter_context(keyed_reads(store, 'native_metadata_observations', observations))
            delivery_ids = [row[0] for observation in observations for row in conn.execute(
                'SELECT delivery_id FROM native_metadata_observations WHERE id=?', (observation,))]
            guards.enter_context(keyed_reads(store, 'metadata_deliveries', delivery_ids))
        rows = conn.execute('SELECT o.*,d.raw_byte_size FROM musical_source_observations s '
                            'JOIN native_metadata_observations o ON o.id=s.observation_id '
                            'JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE s.source_key=? ORDER BY s.field',
                            (source_key,)).fetchall()
        # Conservative bound includes repeated delivery reads; no unbounded provider
        # document is loaded to discover that the profile budget was exceeded.
        if sum(row['raw_byte_size'] for row in rows) > max_bytes:
            raise WorkBudgetExceeded('Source profile input exceeds byte budget')
        # Several selected fields commonly share a delivery. Read that bounded
        # body once, then independently validate every observation reference.
        bodies, result = {}, {}
        for row in rows:
            identity = row['delivery_id']
            if identity not in bodies:
                bodies[identity] = deliveries.response(conn, identity)
            payload = _payload(row, bodies[identity])
            result[payload['field']] = _profile_value(payload)
        return result


def _publish_source(conn, observation_id, payload, provider, recording_id, run_id):
    scope, entity = payload['scope'], payload['entity_id']
    if scope not in ('recording', 'artist', 'release'):
        return
    key = json.dumps([provider, scope, entity], separators=(',', ':'), ensure_ascii=False)
    previous = conn.execute('SELECT observation_id FROM musical_source_observations WHERE source_key=? AND field=?',
                            (key, payload['field'])).fetchone()
    changed = previous is None or _profile_value(read(conn, previous[0])) != _profile_value(payload)
    source = conn.execute('SELECT 1 FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
    if source is None:
        # The legacy payload column is removed by the final owner cutover. The
        # staged adapter keeps only an empty placeholder, never another value.
        has_payload = any(row[1] == 'payload' for row in conn.execute('PRAGMA table_info(rec_musical_sources)'))
        columns, placeholder = (',payload', ",'{}'") if has_payload else ('', '')
        conn.execute(f'INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id{columns}) VALUES (?,?,?,?{placeholder})',
                     (key, provider, scope, entity))
    elif changed:
        conn.execute('UPDATE rec_musical_sources SET revision=revision+1 WHERE source_key=?', (key,))
    conn.execute('INSERT INTO musical_source_observations(source_key,field,observation_id) VALUES (?,?,?) '
                 'ON CONFLICT(source_key,field) DO UPDATE SET observation_id=excluded.observation_id '
                 'WHERE observation_id<>excluded.observation_id', (key, payload['field'], observation_id))
    conn.execute('DELETE FROM rec_musical_links WHERE recording_id=? AND source_key<>? AND delivery_id<>? '
                 'AND EXISTS(SELECT 1 FROM rec_musical_sources s WHERE s.source_key=rec_musical_links.source_key '
                 'AND s.provider=? AND s.scope=?)', (recording_id, key, run_id, provider, scope))
    conn.execute('INSERT INTO rec_musical_links(source_key,recording_id,delivery_id) VALUES (?,?,?) '
                 'ON CONFLICT(source_key,recording_id) DO UPDATE SET delivery_id=excluded.delivery_id '
                 'WHERE delivery_id<>excluded.delivery_id', (key, recording_id, run_id))


def _record(conn, entry_id, provider, observations, *, run_id, now, recording_id):
    """One immutable delivery per provider run/input/source entity; caller owns rollback."""
    prepared, grouped = [], OrderedDict()
    for observation in observations:
        if len(prepared) == MAX_OBSERVATIONS:
            raise ValueError('Native metadata exceeds its observation budget')
        payload = observation.payload()
        if payload['field'] not in FIELDS:
            raise ValueError('Unsupported native metadata field')
        if payload['unit'] is not None and (not isinstance(payload['unit'], str)
                or not 0 < len(payload['unit'].encode()) <= 256):
            raise ValueError('Native metadata unit must be bounded text')
        group = grouped.setdefault((payload['scope'], payload['entity_id']), [])
        group.append(payload)
        prepared.append(payload)
    subject = conn.execute('SELECT video_id FROM catalog_entries WHERE id=? AND recording_id=?',
                           (entry_id, recording_id)).fetchone()
    if subject is None:
        raise ValueError('Native metadata input changed before capture')
    from . import metadata_runtime as runtime
    if runtime.active(conn):
        _record_runtime(conn, entry_id, provider, grouped, run_id=run_id, now=now, recording_id=recording_id)
        return
    captures = [deliveries.Delivery(provider, run_id, scope, entity, now, entry_id, recording_id, subject[0],
                                   response={'format': FORMAT, 'observations': values})
                for (scope, entity), values in grouped.items()]
    ids = deliveries.record_many(conn, captures)
    for delivery, values in zip(ids, grouped.values()):
        for position, payload in enumerate(values):
            if conn.execute('SELECT 1 FROM native_metadata_observations WHERE delivery_id=? AND position=?',
                            (delivery, position)).fetchone() is not None:
                # Reprocessing an older delivery cannot roll current source
                # fields or its current recording links back to that delivery.
                continue
            oid = conn.execute('INSERT INTO native_metadata_observations(delivery_id,position,field,kind,unit) '
                'VALUES (?,?,?,?,?)', (delivery, position, payload['field'], payload['kind'], payload['unit'])).lastrowid
            _publish_source(conn, oid, payload, provider, recording_id, run_id)


def _record_runtime(conn, entry_id, provider, grouped, *, run_id, now, recording_id):
    from . import metadata_runtime_files as files, metadata_runtime as runtime
    runtime.reserve(conn)
    for (scope, entity), values in grouped.items():
        if scope not in ('recording', 'artist', 'release'):
            continue
        key = json.dumps([provider, scope, entity], separators=(',', ':'), ensure_ascii=False)
        source = conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
        previous = files.read(conn, 'native', (key,), source[0]) if source else {}
        normalized = {payload['field']: _profile_value(payload) for payload in values}
        head = conn.execute('SELECT run_id,accepted_at,result_hash FROM metadata_native_delivery_heads WHERE source_key=?',
                            (key,)).fetchone()
        if head is None:
            head = installed_runtime_head(conn, key, previous)
        result_hash = _result_hash(values)
        replay = head is not None and head['run_id'] == run_id
        if replay:
            changed_result = (head['result_hash'] != result_hash if head['result_hash'] is not None else
                any(previous.get('observations', {}).get(name) != value for name, value in normalized.items()))
            if changed_result:
                raise ValueError('A native metadata run cannot change its result')
        elif head is not None and now <= head['accepted_at']:
            # First accepted delivery wins a timestamp tie. Replays retain
            # their original capture time rather than renewing freshness.
            continue
        current = {**previous.get('observations', {}), **normalized}
        changed = not source or current != previous.get('observations', {})
        if changed:
            revision = source[0] + 1 if source else 1
            files.publish(conn, 'native', (key,), revision, {'observations': current, 'raw': values,
                'entry_id': entry_id, 'recording_id': recording_id, 'provider': provider, 'run_id': run_id, 'at': now})
            if source:
                conn.execute('UPDATE rec_musical_sources SET revision=? WHERE source_key=?', (revision, key))
            else:
                conn.execute('INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id,revision) VALUES (?,?,?,?,?)',
                             (key, provider, scope, entity, revision))
        if not replay:
            # This current admission watermark is independent of immutable
            # model inputs. Identical values create neither files nor work.
            retain_runtime_head(conn, key, {'run_id': run_id, 'accepted_at': now, 'result_hash': result_hash})
        conn.execute('DELETE FROM rec_musical_links WHERE recording_id=? AND source_key<>? AND delivery_id<>? '
            'AND EXISTS (SELECT 1 FROM rec_musical_sources s WHERE s.source_key=rec_musical_links.source_key '
            'AND s.provider=? AND s.scope=?)', (recording_id, key, run_id, provider, scope))
        conn.execute('INSERT INTO rec_musical_links(source_key,recording_id,delivery_id) VALUES (?,?,?) '
            'ON CONFLICT(source_key,recording_id) DO UPDATE SET delivery_id=excluded.delivery_id '
            'WHERE delivery_id<>excluded.delivery_id', (key, recording_id, run_id))


def record(conn, entry_id, provider, observations, *, run_id, now, recording_id):
    if not conn.in_transaction:
        raise RuntimeError('Native metadata admission needs an outer transaction')
    conn.execute('SAVEPOINT native_metadata_admission')
    try:
        _record(conn, entry_id, provider, observations, run_id=run_id, now=now, recording_id=recording_id)
    except BaseException:
        conn.execute('ROLLBACK TO native_metadata_admission')
        raise
    finally:
        conn.execute('RELEASE native_metadata_admission')
