"""Detailed prediction evidence in bounded, calculation-owned FAO chunks (#699).

Current scores and original measurement cohorts remain relational. This owner
contains one compact receipt per calculation, never one SQL row per input edge.
"""
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
import tempfile

from . import fao, file_collection, vibe_model_files, vibe_models
from .activity import check_stopping
from .evidence_ledger import canonical

CHUNK_ROWS = 128
MAX_BYTES = 8 * 1024 * 1024


class ProvenanceFiles:
    def __init__(self, root):
        self.root = Path(root)

    def stage_many(self, values):
        grouped, markers, originals = {}, [], set()
        for pid, inputs, artists in values:
            shared = vibe_model_files.is_receipt(inputs)
            value = inputs if shared else {'inputs': sorted(set(inputs)), 'artists': sorted(set(artists))}
            grouped.setdefault(pid // CHUNK_ROWS, {})[str(pid)] = value
            markers.append((pid, hashlib.sha256(canonical(value)).digest() if shared else fingerprint(value['inputs']),
                            len(canonical(value))))
            if not shared:
                originals.update(value['inputs'])
        staged = []
        try:
            for pending in grouped.values():
                pid = int(next(iter(pending)))
                path = self.root / relative(pid)
                source = fao.read(path, max_bytes=MAX_BYTES) if path.is_file() else None
                records = _chunk_at(self.root, pid, content=source)
                records.update(pending)
                encoded = canonical({'format': 1, 'calculations': records})
                staged.append(fao.prepare(path, source.replacing(encoded) if source is not None else encoded))
            return staged, markers, sorted(originals)
        except BaseException:
            for publication in staged:
                publication.close()
            raise


SQL = '''CREATE TABLE prediction_provenance (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id) ON DELETE CASCADE,
    input_fingerprint BLOB NOT NULL CHECK(length(input_fingerprint)=32),
    byte_size INTEGER NOT NULL CHECK(byte_size>0),
    state TEXT NOT NULL CHECK(state IN ('ready','expired'))
)'''


def allocation_sql():
    yield 'CREATE TABLE prediction_id_state (id INTEGER PRIMARY KEY CHECK(id=1),high_water INTEGER NOT NULL CHECK(high_water>=0))'
    for action, side in (('insert', 'NEW'), ('delete', 'OLD')):
        yield (f'CREATE TRIGGER prediction_id_{action} AFTER {action.upper()} ON predictions BEGIN '
               f'UPDATE prediction_id_state SET high_water=max(high_water,{side}.id) WHERE id=1; END')
    yield ("CREATE TRIGGER prediction_id_monotonic BEFORE UPDATE ON prediction_id_state WHEN "
           "NEW.high_water<OLD.high_water BEGIN SELECT RAISE(ABORT,'Calculation allocation cannot go backwards'); END")


def install_allocation(conn, context=None):
    for sql in allocation_sql():
        conn.execute(sql)
    conn.execute('INSERT INTO prediction_id_state SELECT 1,coalesce(max(id),0) FROM predictions')


def validate_allocation(conn):
    from .schema_contract import tokens as _tokens
    from .schema_inspection import definitions
    expected = {sql.split()[2]: sql for sql in allocation_sql()}
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or _tokens(actual[name]) != _tokens(sql):
            raise ValueError('Incompatible calculation allocation')


def next_id(conn):
    row = conn.execute('SELECT high_water FROM prediction_id_state WHERE id=1').fetchone()
    if row is None:
        raise ValueError('Missing calculation allocation state')
    return row[0] + 1


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='prediction_provenance'").fetchone() is not None


def install(conn, context=None):
    if not available(conn):
        conn.execute(SQL)


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_inspection import definitions
    actual = definitions(conn, ['prediction_provenance'])
    if 'prediction_provenance' not in actual or _tokens(actual['prediction_provenance']) != _tokens(SQL):
        raise ValueError('Incompatible prediction provenance owner')


def guards():
    lineage = ("p.lineage_complete=0 OR EXISTS(SELECT 1 FROM prediction_provenance f "
               "WHERE f.prediction_id=p.id AND f.state='ready') OR "
               "(EXISTS(SELECT 1 FROM prediction_inputs i WHERE i.prediction_id=p.id) "
               "AND (p.kind='genre' OR EXISTS(SELECT 1 FROM prediction_artists a WHERE a.prediction_id=p.id)))")
    for action in ('insert', 'update'):
        name = 'recording_vibes_' + action
        yield name, (f'CREATE TRIGGER {name} BEFORE {action.upper()} ON recording_vibes WHEN '
            'NEW.measurement_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM measurements m WHERE '
            'm.id=NEW.measurement_id AND m.source=NEW.source AND m.score=NEW.score AND m.support=NEW.support) '
            'OR NEW.prediction_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM predictions p WHERE '
            'p.id=NEW.prediction_id AND p.score=NEW.score AND p.support=NEW.support AND p.kind=NEW.source '
            f"AND ({lineage})) BEGIN SELECT RAISE(ABORT,'Current vibe does not match its original score'); END")
    yield 'prediction_provenance_expiry', ("CREATE TRIGGER prediction_provenance_expiry BEFORE UPDATE OF state "
        "ON prediction_provenance WHEN NEW.state='expired' AND EXISTS(SELECT 1 FROM recording_vibes "
        "WHERE prediction_id=OLD.prediction_id) BEGIN SELECT RAISE(ABORT,'Current provenance is required'); END")
    yield 'prediction_provenance_immutable', ("CREATE TRIGGER prediction_provenance_immutable BEFORE UPDATE OF "
        "prediction_id,input_fingerprint,byte_size ON prediction_provenance BEGIN "
        "SELECT RAISE(ABORT,'Calculation provenance is immutable'); END")


def install_guards(conn, context=None):
    for name, sql in guards():
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def validate_guards(conn):
    from .schema_contract import tokens as _tokens
    from .schema_inspection import definitions
    expected = dict(guards())
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or _tokens(actual[name]) != _tokens(sql):
            raise ValueError('Incompatible file-owned vibe guard: ' + name)


def lifecycle_sql():
    # One root per original measurement, never one row per calculation edge.
    # Original measurements remain durable inputs under #692 as well.
    yield ('prediction_measurement_roots', 'CREATE TABLE prediction_measurement_roots ('
           'measurement_id INTEGER PRIMARY KEY REFERENCES measurements(id)) WITHOUT ROWID')
    for table in ('prediction_inputs','prediction_artists'):
        for operation in ('insert','update'):
            name = table+'_file_'+operation
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation.upper()} ON {table} '
                'WHEN EXISTS(SELECT 1 FROM prediction_provenance WHERE prediction_id=NEW.prediction_id) '
                "BEGIN SELECT RAISE(ABORT,'Calculation provenance is immutable; inputs require distinct original recordings'); END")


def install_lifecycle(conn, context=None):
    for _,sql in lifecycle_sql():
        conn.execute(sql)


def validate_lifecycle(conn):
    from .schema_contract import tokens as _tokens
    from .schema_inspection import definitions
    expected = dict(lifecycle_sql())
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or _tokens(actual[name]) != _tokens(sql):
            raise ValueError('Incompatible provenance lifecycle: '+name)


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if database:
        path = Path(database).resolve()
        return path.with_name(path.name + '.vibe-provenance')
    return Path(tempfile.gettempdir()) / 'tuneconsole-vibe-provenance' / str(getattr(conn, '_policy_memory_key', id(conn)))


def relative(pid):
    if type(pid) is not int or pid < 1:
        raise ValueError('Invalid calculation identity')
    chunk = pid // CHUNK_ROWS
    return Path(str(chunk // 256), str(chunk) + '.json')


def _chunk(conn, pid, *, content=None):
    return _chunk_at(directory(conn), pid, content=content)


def _chunk_at(root, pid, *, content=None):
    path = root / relative(pid)
    try:
        value = json.loads((content if content is not None else fao.read(path, max_bytes=MAX_BYTES)).bytes)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or value.get('format') != 1 or not isinstance(value.get('calculations'), dict):
        raise fao.CorruptFile('Invalid prediction provenance chunk')
    records = value['calculations']
    if len(records) > CHUNK_ROWS or any(not key.isdigit() or relative(int(key)) != relative(pid) for key in records):
        raise fao.CorruptFile('Prediction provenance differs from its chunk owner')
    return records


def fingerprint(inputs):
    return hashlib.sha256(canonical(sorted(inputs))).digest()


def _model_manifest(conn, generation):
    value = vibe_models.read(conn, generation)
    return {'vibe': value['vibe'], 'inputs': sorted(vibe_model_files.measurement_ids(value)),
            'recordings': {row['recording'] for row in value['inputs']}}


def _read(conn, pid, load, installed, model_load=None):
    row = (conn.execute('SELECT input_fingerprint,byte_size,state FROM prediction_provenance WHERE prediction_id=?', (pid,)).fetchone()
           if installed else None)
    if row is None:
        if not conn.execute('SELECT 1 FROM predictions WHERE id=?', (pid,)).fetchone():
            return None  # Retired history is unavailable, not invented empty lineage.
        # Installed rows remain usable until the explicit conversion reaches them.
        return {'inputs': [r[0] for r in conn.execute('SELECT measurement_id FROM prediction_inputs WHERE prediction_id=? ORDER BY measurement_id', (pid,))],
                'artists': [r[0] for r in conn.execute('SELECT artist_id FROM prediction_artists WHERE prediction_id=? ORDER BY artist_id', (pid,))]}
    if row[2] == 'expired':
        return None
    value = load(pid).get(str(pid))
    if vibe_model_files.is_receipt(value):
        try:
            normalized = vibe_model_files.receipt(value['generation'], value['artists'], value['mean_variance'],
                value['new_track_variance'], conditioning=value.get('conditioning'))
        except (KeyError, TypeError, ValueError) as exc:
            raise fao.CorruptFile('Invalid shared model receipt conditioning') from exc
        if normalized != value:
            raise fao.CorruptFile('Invalid shared model receipt conditioning')
        if (len(canonical(value)) != row[1] or hashlib.sha256(canonical(value)).digest() != row[0]
                or vibe_models.linked(conn, pid) != value['generation']):
            raise fao.CorruptFile('Required shared model receipt is missing or corrupt')
        model = model_load(value['generation']) if model_load else _model_manifest(conn, value['generation'])
        prediction = vibe_models.subject(conn, pid)
        if (not prediction or prediction[0] != model['vibe']
                or prediction[1] in model['recordings']):
            raise fao.CorruptFile('Shared model does not match its prediction conditioning')
        return {**value, 'inputs': model['inputs']}
    if (not isinstance(value, dict) or set(value) != {'inputs', 'artists'}
            or len(canonical(value)) != row[1] or fingerprint(value['inputs']) != row[0]):
        raise fao.CorruptFile('Required calculation provenance is missing or corrupt')
    return value


def read(conn, pid):
    return _read(conn, pid, lambda identity: _chunk(conn, identity), available(conn))


def read_many(conn, identities):
    """Verify each calculation while retaining at most one shared file chunk."""
    chunk, records = None, None
    installed = available(conn)
    manifests = OrderedDict()
    def model_load(generation):
        if generation not in manifests:
            manifests[generation] = _model_manifest(conn, generation)
            if len(manifests) > 8:
                manifests.popitem(last=False)
        manifests.move_to_end(generation)
        return manifests[generation]
    def load(pid):
        nonlocal chunk, records
        key = pid // CHUNK_ROWS
        if key != chunk:
            records = _chunk(conn, pid)
            chunk = key
        return records
    for pid in identities:
        yield pid, _read(conn, pid, load, installed, model_load)


def same_inputs(conn, pid, inputs):
    row = (conn.execute('SELECT input_fingerprint,state FROM prediction_provenance WHERE prediction_id=?', (pid,)).fetchone()
           if available(conn) else None)
    if row is not None:
        if row[1] != 'ready':
            raise fao.CorruptFile('Current calculation provenance expired while referenced')
        return row[0] == fingerprint(inputs)
    return {r[0] for r in conn.execute('SELECT measurement_id FROM prediction_inputs WHERE prediction_id=?', (pid,))} == set(inputs)


def same_receipt(conn, pid, receipt):
    return vibe_models.same_receipt(conn, pid, receipt)


def stage_many(conn, values):
    """Prepare complete chunks against a pinned snapshot, before SQL publication.

    The preparation planner validates original subjects and calculation identity.
    Snapshot revisions fence these inputs and the allocation high-water mark.
    The physical identity check additionally fences repacking/conversion races.
    """
    grouped, markers, originals = {}, [], set()
    for pid, inputs, artists in values:
        shared = vibe_model_files.is_receipt(inputs)
        value = inputs if shared else {'inputs':sorted(set(inputs)), 'artists':sorted(set(artists))}
        grouped.setdefault(pid//CHUNK_ROWS,{})[str(pid)] = value
        markers.append((pid,hashlib.sha256(canonical(value)).digest() if shared else fingerprint(value['inputs']),len(canonical(value))))
        if not shared:
            originals.update(value['inputs'])
    staged = []
    try:
        for chunk, pending in grouped.items():
            pid = int(next(iter(pending)))
            path = directory(conn)/relative(pid)
            source = fao.read(path, max_bytes=MAX_BYTES) if path.is_file() else None
            records = _chunk(conn,pid,content=source)
            records.update(pending)
            encoded = canonical({'format':1,'calculations':records})
            staged.append(fao.prepare(path, source.replacing(encoded) if source is not None else encoded))
        # Existing roots need no repeated write during the guarded publication.
        roots, ordered = [], sorted(originals)
        for start in range(0, len(ordered), 128):
            check_stopping()
            batch = ordered[start:start + 128]
            qs = ','.join('?' for _ in batch)
            existing = {row[0] for row in conn.execute(
                f'SELECT measurement_id FROM prediction_measurement_roots WHERE measurement_id IN ({qs})', batch)}
            roots.extend(mid for mid in batch if mid not in existing)
        return staged, markers, roots
    except BaseException:
        for publication in staged:
            publication.close()
        raise


def publish_many(conn, values):
    if not conn.in_transaction:
        raise RuntimeError('Provenance publication requires the calculation writer reservation')
    grouped = {}
    shared = []
    for pid, inputs, artists in values:
        if vibe_model_files.is_receipt(inputs):
            shared.append((pid, inputs, artists))
            continue
        prediction = conn.execute('SELECT vibe,recording_id,lineage_complete,kind FROM predictions WHERE id=?', (pid,)).fetchone()
        if not prediction:
            raise ValueError('Unknown prediction owner')
        value = {'inputs': sorted(set(inputs)), 'artists': sorted(set(artists))}
        if any(type(i) is not int or i < 1 for i in value['inputs'] + value['artists']):
            raise ValueError('Invalid calculation provenance subjects')
        if prediction[2] and (not value['inputs'] or prediction[3] != 'genre' and not value['artists']):
            raise ValueError('Complete calculation requires original inputs and conditioning')
        subjects = set()
        for mid in value['inputs']:
            original = conn.execute('SELECT vibe,recording_id FROM measurements WHERE id=?', (mid,)).fetchone()
            if original is None or original[0] != prediction[0] or original[1] == prediction[1] or original[1] in subjects:
                raise ValueError('Prediction inputs require distinct original recordings of the same vibe')
            subjects.add(original[1])
            conn.execute('INSERT OR IGNORE INTO prediction_measurement_roots VALUES (?)',(mid,))
        for aid in value['artists']:
            if not conn.execute('SELECT 1 FROM artists WHERE id=?', (aid,)).fetchone():
                raise ValueError('Unknown conditioning artist')
        pending = grouped.setdefault(pid // CHUNK_ROWS, {})
        if str(pid) in pending and pending[str(pid)] != value:
            raise ValueError('Conflicting calculation provenance')
        pending[str(pid)] = value
    for chunk, values in grouped.items():
        pid = int(next(iter(values)))
        records = _chunk(conn, pid)
        for identity, value in values.items():
            if conn.execute('SELECT 1 FROM prediction_provenance WHERE prediction_id=?', (int(identity),)).fetchone():
                if read(conn, int(identity)) != value:
                    raise ValueError('Calculation provenance cannot change')
            records[identity] = value
        encoded = canonical({'format': 1, 'calculations': records})
        fao.write(directory(conn) / relative(pid), encoded)
        conn.executemany('INSERT INTO prediction_provenance VALUES (?,?,?,\'ready\') '
                         'ON CONFLICT(prediction_id) DO NOTHING',
                         [(int(identity), fingerprint(value['inputs']), len(canonical(value))) for identity, value in values.items()])
    if shared:
        # Rebuilds preserve compact shared identities rather than expanding each
        # manifest into an ordinary contributor graph. Borrowed FAO roots are
        # pinned for the rebuild's full lifetime by file_owners.readers.
        vibe_models.service(conn, ProvenanceFiles(directory(conn))).publish_receipts(shared)


def prepare_batch(conn, limit=128):
    """Explicit resumable conversion; files are verified before removing SQL edges."""
    rows = conn.execute('SELECT p.id FROM predictions p LEFT JOIN prediction_provenance f ON f.prediction_id=p.id '
                        'WHERE f.prediction_id IS NULL ORDER BY p.id LIMIT ?', (limit,)).fetchall()
    values = []
    for (pid,) in rows:
        saved = read(conn, pid)
        values.append((pid, saved['inputs'], saved['artists']))
    publish_many(conn, values)
    for pid, inputs, artists in values:
        if read(conn, pid) != {'inputs': inputs, 'artists': artists}:
            raise fao.CorruptFile('Converted provenance differs from original subjects')
        conn.execute('DELETE FROM prediction_inputs WHERE prediction_id=?', (pid,))
        conn.execute('DELETE FROM prediction_artists WHERE prediction_id=?', (pid,))
    return len(rows)


def references(conn):
    if available(conn):
        for chunk, in conn.execute("SELECT DISTINCT prediction_id/128 FROM prediction_provenance WHERE state='ready'"):
            yield directory(conn) / relative(chunk*CHUNK_ROWS+1)


def maintain(conn, now, limit=128):
    """Expire only superseded calculations, thirty days from their saved mtime."""
    rows = conn.execute("SELECT f.prediction_id FROM prediction_provenance f JOIN predictions p ON p.id=f.prediction_id "
        "WHERE f.state='ready' AND p.predicted_at<? AND NOT EXISTS(SELECT 1 FROM recording_vibes r "
        "WHERE r.recording_id=p.recording_id AND r.vibe=p.vibe AND r.prediction_id=p.id) "
        "ORDER BY p.predicted_at,p.id LIMIT ?", (now-30*86400, limit)).fetchall()
    for pid, in rows:
        conn.execute("UPDATE prediction_provenance SET state='expired' WHERE prediction_id=?", (pid,))
    return len(rows)


def collect(conn, now, limit=16):
    """Repack committed expiry only, under the owner's snapshot fence.

    Never remove bytes before the SQL expiry transaction commits. A failed or
    interrupted repack leaves either the old exact chunk or its verified subset.
    """
    if conn.in_transaction:
        raise RuntimeError('Provenance collection requires committed expiry')
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Provenance collection limit must be 1 to 128')
    root = directory(conn)
    with file_collection.collection(conn, root, name='.collection.json', valid_shard=_shard) as work, file_collection.collector(root) as admitted:
        if not admitted:
            return 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            return _collect_reserved(conn,root,work,now,limit)
        finally:
            conn.rollback()


def _shard(name):
    return isinstance(name, str) and name.isascii() and name.isdigit() and str(int(name)) == name


def _collect_reserved(conn,root,work,now,limit):
    examined = 0
    for _ in range(limit):
        path = work.next()
        if path is None or path.suffix != '.json' or not _shard(path.stem):
            continue
        group = int(path.stem)
        pid = group * CHUNK_ROWS + 1
        if path.relative_to(root) != relative(pid):
            continue
        examined += 1
        content = fao.read(path, max_bytes=MAX_BYTES)
        records = _chunk(conn, pid, content=content)
        ready = {str(r[0]) for r in conn.execute("SELECT prediction_id FROM prediction_provenance "
            "WHERE state='ready' AND prediction_id>=? AND prediction_id<?", (group*CHUNK_ROWS, (group+1)*CHUNK_ROWS))}
        owned = {str(r[0]) for r in conn.execute('SELECT prediction_id FROM prediction_provenance '
            'WHERE prediction_id>=? AND prediction_id<?', (group*CHUNK_ROWS, (group+1)*CHUNK_ROWS))}
        # Orphans from a rolled-back publication have a thirty-day grace.
        if not ready and records.keys() - owned and not (content.info.ctime_ns / 1e9 <= now - 30*86400):
            continue
        retained = {key: value for key, value in records.items() if key in ready}
        if ready - retained.keys():
            raise fao.CorruptFile('Required calculation provenance is missing')
        if retained and retained != records:
            fao.write(path, content.replacing(canonical({'format': 1, 'calculations': retained})))
        elif not retained:
            fao.delete(path, missing_ok=True)
    work.save()
    return examined
