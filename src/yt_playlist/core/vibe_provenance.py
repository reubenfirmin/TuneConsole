"""Detailed prediction evidence in bounded, calculation-owned FAO chunks (#699).

Current scores and original measurement cohorts remain relational. This owner
contains one compact receipt per calculation, never one SQL row per input edge.
"""
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
import tempfile
from contextlib import contextmanager, nullcontext

from . import fao, file_collection, file_pack, vibe_model_files, vibe_models
from .activity import check_stopping
from .evidence_ledger import canonical
from .file_receipts import PROVENANCE_HEAD as SQL, has_native_provenance_heads, native_provenance_definition
from .vibe_model_queries import retain_measurements

CHUNK_ROWS = file_pack.MAX_ROWS
MAX_BYTES = file_pack.MAX_BYTES
HIGH_WATER = 'vibe_prediction_high_water'


class ProvenanceFiles:
    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def describe(values):
        grouped, originals, seen = {}, set(), set()
        for pid, inputs, artists in values:
            relative(pid)
            if pid in seen:
                raise ValueError('Duplicate calculation provenance')
            seen.add(pid)
            shared = vibe_model_files.is_receipt(inputs)
            value = inputs if shared else {'inputs': sorted(set(inputs)), 'artists': sorted(set(artists))}
            if not shared:
                if any(type(identity) is not int or identity < 1 for identity in value['inputs'] + value['artists']):
                    raise ValueError('Invalid calculation provenance subjects')
                originals.update(value['inputs'])
            grouped.setdefault(pid // CHUNK_ROWS, {})[str(pid)] = value
        records = []
        for calculations in grouped.values():
            raw = canonical({'format': 1, 'calculations': calculations})
            file_pack.validate(raw, len(calculations), max_bytes=MAX_BYTES, max_rows=CHUNK_ROWS)
            markers = [(int(pid), hashlib.sha256(canonical(value)).digest()
                        if vibe_model_files.is_receipt(value) else fingerprint(value['inputs']),
                        len(canonical(value))) for pid, value in sorted(calculations.items(), key=lambda row: int(row[0]))]
            records.append({'raw': raw, 'digest': hashlib.sha256(raw).digest(), 'size': len(raw),
                            'markers': markers})
        return records, sorted(originals)

    def stage_reserved(self, records):
        staged, markers = [], []
        try:
            for record in records:
                raw = record['raw']
                if (not 0 < len(raw) <= MAX_BYTES or len(raw) != record['size']
                        or hashlib.sha256(raw).digest() != record['digest']):
                    raise ValueError('Prediction provenance changed after reservation')
                value = json.loads(raw)
                calculations = _calculations(value)
                expected, _roots = self.describe((int(pid), calculation if vibe_model_files.is_receipt(calculation)
                    else calculation['inputs'], calculation['artists']) for pid, calculation in calculations.items())
                if len(expected) != 1 or any(expected[0][key] != record[key]
                                            for key in ('raw', 'digest', 'size', 'markers')):
                    raise ValueError('Prediction provenance changed after reservation')
                staged.append(fao.prepare(self.root / file_relative(record['file_id']), raw))
                markers.extend((*marker, record['file_id']) for marker in record['markers'])
            return staged, markers
        except BaseException:
            for publication in staged:
                publication.close()
            raise

    def read(self, pid, file_id, digest, size):
        content = fao.read(self.root / file_relative(file_id), max_bytes=MAX_BYTES)
        with content as stream:
            try:
                raw = file_pack.PackReader(stream, header=b'', max_bytes=MAX_BYTES,
                                          max_pack_bytes=MAX_BYTES).read(0, size, digest.hex())
            except fao.CorruptFile as exc:
                raise fao.CorruptFile('Prediction provenance differs from its reserved bytes') from exc
        return _chunk_at(self.root, pid, content=fao.Content(raw), file_id=file_id)

    @contextmanager
    def garbage(self, *, limit=16):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('Provenance collection limit must be 1 to 128')
        with file_collection.collector(self.root) as admitted:
            if not admitted:
                yield iter(())
                return
            with file_collection.collection(self, self.root, name='.record-collection.json',
                    valid_shard=lambda name: _shard(name) or len(name) == 2 and all(c in '0123456789abcdef' for c in name),
                    prefixes=('', 'by-id')) as work:
                def candidates():
                    for _ in range(limit):
                        path = work.next()
                        if path is None or path.suffix != '.json' or not _shard(path.stem):
                            continue
                        identity = int(path.stem)
                        native = path.relative_to(self.root) == file_relative(identity) if identity else False
                        if not native and path.relative_to(self.root) != relative(identity * CHUNK_ROWS + 1):
                            continue
                        try:
                            content = fao.read(path, max_bytes=MAX_BYTES)
                        except FileNotFoundError:
                            continue
                        yield path, content, identity if native else None, identity if not native else None
                try:
                    yield candidates()
                finally:
                    work.save()

    @staticmethod
    def prune(path, content, *, file_id, ready, owned, before):
        if file_id is not None:
            if not ready and content.info.ctime_ns / 1e9 <= before:
                fao.delete(path, expected=content.info.identity, missing_ok=True)
            return
        records = _calculations(json.loads(content.bytes))
        if any(int(key) // CHUNK_ROWS != int(path.stem) for key in records):
            raise fao.CorruptFile('Prediction provenance differs from its chunk owner')
        if not ready and records.keys() - owned and content.info.ctime_ns / 1e9 > before:
            return
        retained = {key: value for key, value in records.items() if key in ready}
        if ready - retained.keys():
            raise fao.CorruptFile('Required calculation provenance is missing')
        if retained and retained != records:
            fao.write(path, content.replacing(canonical({'format': 1, 'calculations': retained})))
        elif not retained:
            fao.delete(path, expected=content.info.identity, missing_ok=True)

    @contextmanager
    def history(self):
        with file_collection.collector(self.root) as admitted:
            if not admitted:
                yield None
                return
            cursor = self.root / '.history-collection.json'
            after = json.loads(fao.read(cursor).bytes)['after'] if cursor.exists() else 0
            if type(after) is not int or after < 0:
                raise ValueError('Invalid prediction history cursor')
            yield after

    def save_history(self, after):
        fao.write(self.root / '.history-collection.json', canonical({'after': after}))

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
                file_pack.validate(encoded, len(records), max_bytes=MAX_BYTES, max_rows=CHUNK_ROWS)
                staged.append(fao.prepare(path, source.replacing(encoded) if source is not None else encoded))
            return staged, markers, sorted(originals)
        except BaseException:
            for publication in staged:
                publication.close()
            raise


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
    # Track changes to predictions in the snapshot as well as the sequence.
    return conn.execute("SELECT max(coalesce((SELECT max(id) FROM predictions),0),"
                        "coalesce((SELECT seq FROM sqlite_sequence WHERE name='predictions'),0))+1").fetchone()[0]


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema "
                        "WHERE name IN ('prediction_provenance','predictions_provenance_immutable')").fetchone() is not None


def install(conn, context=None):
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='prediction_provenance'").fetchone():
        conn.execute(SQL)


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_inspection import definitions
    actual = definitions(conn, ['prediction_provenance'])
    if ('prediction_provenance' not in actual
            or _tokens(actual['prediction_provenance']) not in (_tokens(SQL), _tokens(native_provenance_definition(SQL)))):
        raise ValueError('Incompatible prediction provenance owner')


def current_fit_guard(action, lineage):
    name = 'recording_vibes_' + action
    return (f'CREATE TRIGGER {name} BEFORE {action.upper()} ON recording_vibes WHEN '
            'NEW.measurement_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM measurements m WHERE '
            'm.id=NEW.measurement_id AND m.source=NEW.source AND m.score=NEW.score AND m.support=NEW.support) '
            'OR NEW.prediction_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM predictions p WHERE '
            'p.id=NEW.prediction_id AND p.score=NEW.score AND p.support=NEW.support AND p.kind=NEW.source '
            f"AND ({lineage})) BEGIN SELECT RAISE(ABORT,'Current vibe does not match its original score'); END")


def guards():
    lineage = ("p.lineage_complete=0 OR EXISTS(SELECT 1 FROM prediction_provenance f "
               "WHERE f.prediction_id=p.id AND f.state='ready') OR "
               "(EXISTS(SELECT 1 FROM prediction_inputs i WHERE i.prediction_id=p.id) "
               "AND (p.kind='genre' OR EXISTS(SELECT 1 FROM prediction_artists a WHERE a.prediction_id=p.id)))")
    for action in ('insert', 'update'):
        yield 'recording_vibes_' + action, current_fit_guard(action, lineage)
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


def file_relative(identity):
    if type(identity) is not int or identity < 1:
        raise ValueError('Invalid provenance file identity')
    return Path('by-id', f'{identity % 256:02x}', str(identity) + '.json')


def _chunk(conn, pid, *, content=None, file_id=None):
    if file_id is not None:
        row = conn.execute("SELECT content_digest,byte_size,file_status FROM collection_file_writes "
                           "WHERE kind='prediction-provenance' AND file_address=?",
                           (file_id,)).fetchone()
        if row is None or row[2] != 'ready':
            raise fao.CorruptFile('Required calculation file is not ready')
        return ProvenanceFiles(directory(conn)).read(pid, file_id, bytes.fromhex(row[0]), row[1])
    return _chunk_at(directory(conn), pid, content=content, file_id=file_id)


def _chunk_at(root, pid, *, content=None, file_id=None):
    path = root / (file_relative(file_id) if file_id is not None else relative(pid))
    try:
        value = json.loads((content if content is not None else fao.read(path, max_bytes=MAX_BYTES)).bytes)
    except FileNotFoundError:
        return {}
    records = _calculations(value)
    if any(relative(int(key)) != relative(pid) for key in records):
        raise fao.CorruptFile('Prediction provenance differs from its chunk owner')
    return records


def _calculations(value):
    if not isinstance(value, dict) or value.get('format') != 1 or not isinstance(value.get('calculations'), dict):
        raise fao.CorruptFile('Invalid prediction provenance chunk')
    records = value['calculations']
    if len(records) > CHUNK_ROWS or any(not _shard(key) or int(key) < 1 for key in records):
        raise fao.CorruptFile('Prediction provenance differs from its chunk owner')
    return records


def fingerprint(inputs):
    return hashlib.sha256(canonical(sorted(inputs))).digest()


def _model_manifest(conn, generation):
    value = vibe_models.read(conn, generation)
    return {'vibe': value['vibe'], 'inputs': sorted(vibe_model_files.measurement_ids(value)),
            'recordings': {row['recording'] for row in value['inputs']}}


def _read(conn, pid, load, model_load=None, *, receipt_only=False, legacy=False):
    if legacy:
        row = conn.execute('SELECT f.input_fingerprint,f.byte_size,f.state,f.file_id,g.generation '
                           'FROM prediction_provenance f LEFT JOIN prediction_model_generations g USING(prediction_id) '
                           'WHERE f.prediction_id=?', (pid,)).fetchone()
    else:
        row = conn.execute('SELECT provenance_fingerprint,provenance_bytes,provenance_state,provenance_file_id,'
                           'model_generation FROM predictions WHERE id=?', (pid,)).fetchone()
    if row is None:
        return None
    if row[2] is None:
        return None if receipt_only else {'inputs': [], 'artists': []}
    if row[2] == 'expired':
        return None
    value = load(pid, row[3]).get(str(pid))
    if vibe_model_files.is_receipt(value):
        try:
            normalized = vibe_model_files.receipt(value['generation'], value['artists'], value['mean_variance'],
                value['new_track_variance'], conditioning=value.get('conditioning'))
        except (KeyError, TypeError, ValueError) as exc:
            raise fao.CorruptFile('Invalid shared model receipt conditioning') from exc
        if normalized != value:
            raise fao.CorruptFile('Invalid shared model receipt conditioning')
        if (len(canonical(value)) != row[1] or hashlib.sha256(canonical(value)).digest() != row[0]
                or row[4] is None or row[4].hex() != value['generation']):
            raise fao.CorruptFile('Required shared model receipt is missing or corrupt')
        if receipt_only:
            return normalized
        model = model_load(value['generation']) if model_load else _model_manifest(conn, value['generation'])
        prediction = vibe_models.subject(conn, pid)
        if (not prediction or prediction[0] != model['vibe']
                or prediction[1] in model['recordings']):
            raise fao.CorruptFile('Shared model does not match its prediction conditioning')
        return {**value, 'inputs': model['inputs']}
    if receipt_only:
        return None
    if (not isinstance(value, dict) or set(value) != {'inputs', 'artists'}
            or len(canonical(value)) != row[1] or fingerprint(value['inputs']) != row[0]):
        raise fao.CorruptFile('Required calculation provenance is missing or corrupt')
    return value


def read(conn, pid):
    return _read(conn, pid, lambda identity, file_id: _chunk(conn, identity, file_id=file_id))


def conditioning(conn, pid, *, file_reads=nullcontext):
    """Read the verified target covariates even if its posterior is unavailable."""
    def load(identity, file_id):
        with file_reads(file_id):
            return _chunk(conn, identity, file_id=file_id)

    value = _read(conn, pid, load, receipt_only=True)
    return value.get('conditioning') if value else None


def read_many(conn, identities):
    """Verify each calculation while retaining at most one shared file chunk."""
    chunk, records = None, None
    manifests = OrderedDict()
    def model_load(generation):
        if generation not in manifests:
            manifests[generation] = _model_manifest(conn, generation)
            if len(manifests) > 8:
                manifests.popitem(last=False)
        manifests.move_to_end(generation)
        return manifests[generation]
    def load(pid, file_id):
        nonlocal chunk, records
        key = pid // CHUNK_ROWS, file_id
        if key != chunk:
            records = _chunk(conn, pid, file_id=file_id) if file_id is not None else _chunk(conn, pid)
            chunk = key
        return records
    for pid in identities:
        yield pid, _read(conn, pid, load, model_load)


def same_inputs(conn, pid, inputs):
    row = conn.execute('SELECT provenance_fingerprint,provenance_state FROM predictions WHERE id=? '
                       'AND provenance_state IS NOT NULL', (pid,)).fetchone()
    if row is None:
        return False
    if row[1] != 'ready':
        raise fao.CorruptFile('Current calculation provenance expired while referenced')
    return row[0] == fingerprint(inputs)


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
            file_pack.validate(encoded, len(records), max_bytes=MAX_BYTES, max_rows=CHUNK_ROWS)
            staged.append(fao.prepare(path, source.replacing(encoded) if source is not None else encoded))
        # Existing roots need no repeated write during the guarded publication.
        roots, ordered = [], sorted(originals)
        for start in range(0, len(ordered), 128):
            check_stopping()
            batch = ordered[start:start + 128]
            qs = ','.join('?' for _ in batch)
            existing = {row[0] for row in conn.execute(
                f'SELECT id FROM measurements WHERE used_by_model=1 AND id IN ({qs})', batch)}
            roots.extend(mid for mid in batch if mid not in existing)
        return staged, markers, roots
    except BaseException:
        for publication in staged:
            publication.close()
        raise


def publish_many(conn, values, *, legacy=False):
    if not conn.in_transaction:
        raise RuntimeError('Provenance publication requires the calculation writer reservation')
    grouped, originals = {}, set()
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
            originals.add(mid)
        for aid in value['artists']:
            if not conn.execute('SELECT 1 FROM artists WHERE id=?', (aid,)).fetchone():
                raise ValueError('Unknown conditioning artist')
        pending = grouped.setdefault(pid // CHUNK_ROWS, {})
        if str(pid) in pending and pending[str(pid)] != value:
            raise ValueError('Conflicting calculation provenance')
        pending[str(pid)] = value
    retain_measurements(conn, originals)
    markers = ('SELECT prediction_id FROM prediction_provenance WHERE prediction_id=?' if legacy else
               'SELECT id FROM predictions WHERE id=? AND provenance_state IS NOT NULL')
    for values in grouped.values():
        pid = int(next(iter(values)))
        records = _chunk(conn, pid)
        for identity, value in values.items():
            if conn.execute(markers, (int(identity),)).fetchone():
                if _read(conn, int(identity), lambda p, f: _chunk(conn, p, file_id=f), legacy=legacy) != value:
                    raise ValueError('Calculation provenance cannot change')
            records[identity] = value
        encoded = canonical({'format': 1, 'calculations': records})
        file_pack.validate(encoded, len(records), max_bytes=MAX_BYTES, max_rows=CHUNK_ROWS)
        fao.write(directory(conn) / relative(pid), encoded)
        markers_to_write = [(fingerprint(value['inputs']), len(canonical(value)), int(identity))
                            for identity, value in values.items()]
        if legacy:
            conn.executemany("INSERT INTO prediction_provenance(input_fingerprint,byte_size,prediction_id,state) "
                             "VALUES (?,?,?,'ready') ON CONFLICT(prediction_id) DO NOTHING", markers_to_write)
        else:
            conn.executemany("UPDATE predictions SET provenance_fingerprint=?,provenance_bytes=?,"
                             "provenance_state='ready' WHERE id=? AND provenance_state IS NULL", markers_to_write)
    if shared:
        # Version changes preserve compact shared identities instead of expanding each
        # manifest into an ordinary contributor graph. Borrowed FAO roots are
        # pinned for the rebuild's full lifetime by file_owners.readers.
        vibe_models.service(conn, ProvenanceFiles(directory(conn))).publish_receipts(shared)


def has_sql_links(conn):
    return bool(conn.execute('SELECT 1 FROM prediction_inputs LIMIT 1').fetchone()
                or conn.execute('SELECT 1 FROM prediction_artists LIMIT 1').fetchone())


def convert_sql_links(conn, limit=64):
    """Move only predictions that still own SQL lineage, before retiring its tables."""
    rows = conn.execute('SELECT prediction_id FROM prediction_inputs UNION '
                        'SELECT prediction_id FROM prediction_artists ORDER BY 1 LIMIT ?', (limit,)).fetchall()
    values = []
    for pid, in rows:
        inputs = [row[0] for row in conn.execute('SELECT measurement_id FROM prediction_inputs '
                                                'WHERE prediction_id=? ORDER BY measurement_id', (pid,))]
        artists = [row[0] for row in conn.execute('SELECT artist_id FROM prediction_artists '
                                                 'WHERE prediction_id=? ORDER BY artist_id', (pid,))]
        values.append((pid, inputs, artists))
    publish_many(conn, values, legacy=True)
    for pid, inputs, artists in values:
        if _read(conn, pid, lambda p, f: _chunk(conn, p, file_id=f), legacy=True) != {'inputs': inputs, 'artists': artists}:
            raise fao.CorruptFile('Converted provenance differs from original subjects')
        conn.execute('DELETE FROM prediction_inputs WHERE prediction_id=?', (pid,))
        conn.execute('DELETE FROM prediction_artists WHERE prediction_id=?', (pid,))
    return len(values)


def references(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='prediction_provenance'").fetchone():
        column = 'file_id' if has_native_provenance_heads(conn) else 'NULL'
        sql = 'SELECT DISTINCT prediction_id/128,' + column + " FROM prediction_provenance WHERE state='ready'"
    else:
        sql = "SELECT DISTINCT id/128,provenance_file_id FROM predictions WHERE provenance_state='ready'"
    for chunk, identity in conn.execute(sql):
        yield directory(conn) / (file_relative(identity) if identity is not None else relative(chunk*CHUNK_ROWS+1))


def maintain(conn, now, limit=128):
    """Expire only superseded calculations, thirty days from their saved mtime."""
    rows = conn.execute("SELECT p.id FROM predictions p "
        "WHERE p.provenance_state='ready' AND p.predicted_at<? AND NOT EXISTS(SELECT 1 FROM recording_vibes r "
        "WHERE r.recording_id=p.recording_id AND r.vibe=p.vibe AND r.prediction_id=p.id) "
        "ORDER BY p.predicted_at,p.id LIMIT ?", (now-30*86400, limit)).fetchall()
    for pid, in rows:
        conn.execute("UPDATE predictions SET provenance_state='expired' WHERE id=?", (pid,))
    return len(rows)


def _shard(name):
    return isinstance(name, str) and name.isascii() and name.isdigit() and str(int(name)) == name
