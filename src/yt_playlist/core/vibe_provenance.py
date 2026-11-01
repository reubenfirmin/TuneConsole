"""Detailed prediction evidence in bounded, calculation-owned FAO chunks (#699).

Current scores and original measurement cohorts remain relational. This owner
contains one compact receipt per calculation, never one SQL row per input edge.
"""
import hashlib
import json
from pathlib import Path
import tempfile

from . import fao
from .evidence_ledger import canonical

CHUNK_ROWS = 128
MAX_BYTES = 8 * 1024 * 1024
SQL = '''CREATE TABLE prediction_provenance (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id) ON DELETE CASCADE,
    input_fingerprint BLOB NOT NULL CHECK(length(input_fingerprint)=32),
    byte_size INTEGER NOT NULL CHECK(byte_size>0),
    state TEXT NOT NULL CHECK(state IN ('ready','expired'))
)'''


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='prediction_provenance'").fetchone() is not None


def install(conn, context=None):
    if not available(conn):
        conn.execute(SQL)


def validate(conn):
    from .migrations.current import _tokens
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='prediction_provenance'").fetchone()
    if row is None or _tokens(row[0]) != _tokens(SQL):
        raise ValueError('Incompatible prediction provenance owner')


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


def _chunk(conn, pid):
    path = directory(conn) / relative(pid)
    try:
        value = json.loads(fao.read_bytes(path, max_bytes=MAX_BYTES))
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


def read(conn, pid):
    row = conn.execute('SELECT input_fingerprint,byte_size,state FROM prediction_provenance WHERE prediction_id=?', (pid,)).fetchone()
    if row is None:
        # Installed rows remain usable until the explicit conversion reaches them.
        return {'inputs': [r[0] for r in conn.execute('SELECT measurement_id FROM prediction_inputs WHERE prediction_id=? ORDER BY measurement_id', (pid,))],
                'artists': [r[0] for r in conn.execute('SELECT artist_id FROM prediction_artists WHERE prediction_id=? ORDER BY artist_id', (pid,))]}
    if row[2] == 'expired':
        return None
    value = _chunk(conn, pid).get(str(pid))
    if (not isinstance(value, dict) or set(value) != {'inputs', 'artists'}
            or len(canonical(value)) != row[1] or fingerprint(value['inputs']) != row[0]):
        raise fao.CorruptFile('Required calculation provenance is missing or corrupt')
    return value


def same_inputs(conn, pid, inputs):
    row = (conn.execute('SELECT input_fingerprint,state FROM prediction_provenance WHERE prediction_id=?', (pid,)).fetchone()
           if available(conn) else None)
    if row is not None:
        if row[1] != 'ready':
            raise fao.CorruptFile('Current calculation provenance expired while referenced')
        return row[0] == fingerprint(inputs)
    return {r[0] for r in conn.execute('SELECT measurement_id FROM prediction_inputs WHERE prediction_id=?', (pid,))} == set(inputs)


def publish_many(conn, values):
    if not conn.in_transaction:
        raise RuntimeError('Provenance publication requires the calculation writer reservation')
    grouped = {}
    for pid, inputs, artists in values:
        if not conn.execute('SELECT 1 FROM predictions WHERE id=?', (pid,)).fetchone():
            raise ValueError('Unknown prediction owner')
        value = {'inputs': sorted(set(inputs)), 'artists': sorted(set(artists))}
        if not value['inputs'] or any(type(i) is not int or i < 1 for i in value['inputs'] + value['artists']):
            raise ValueError('Invalid calculation provenance subjects')
        grouped.setdefault(pid // CHUNK_ROWS, {})[str(pid)] = value
    for chunk, values in grouped.items():
        pid = int(next(iter(values)))
        records = _chunk(conn, pid)
        for identity, value in values.items():
            if conn.execute('SELECT 1 FROM prediction_provenance WHERE prediction_id=?', (int(identity),)).fetchone():
                if read(conn, int(identity)) != value:
                    raise ValueError('Calculation provenance cannot change')
            records[identity] = value
        encoded = canonical({'format': 1, 'calculations': records})
        fao.write_bytes(directory(conn) / relative(pid), encoded, max_bytes=MAX_BYTES)
        conn.executemany('INSERT INTO prediction_provenance VALUES (?,?,?,\'ready\') '
                         'ON CONFLICT(prediction_id) DO NOTHING',
                         [(int(identity), fingerprint(value['inputs']), len(canonical(value))) for identity, value in values.items()])


def prepare_batch(conn, limit=128):
    """Explicit resumable conversion; files are verified before removing SQL edges."""
    rows = conn.execute('SELECT p.id FROM predictions p LEFT JOIN prediction_provenance f ON f.prediction_id=p.id '
                        'WHERE f.prediction_id IS NULL ORDER BY p.id LIMIT ?', (limit,)).fetchall()
    values = [(row[0], *read(conn, row[0]).values()) for row in rows]
    # Empty original lineage remains explicitly incomplete in its prediction row.
    values = [row for row in values if row[1]]
    publish_many(conn, values)
    for pid, inputs, artists in values:
        if read(conn, pid) != {'inputs': inputs, 'artists': artists}:
            raise fao.CorruptFile('Converted provenance differs from original subjects')
        conn.execute('DELETE FROM prediction_inputs WHERE prediction_id=?', (pid,))
        conn.execute('DELETE FROM prediction_artists WHERE prediction_id=?', (pid,))
    return len(rows)


def references(conn):
    if available(conn):
        for pid, in conn.execute("SELECT prediction_id FROM prediction_provenance WHERE state='ready' ORDER BY prediction_id"):
            yield directory(conn) / relative(pid)


def maintain(conn, now, limit=128):
    """Expire only superseded calculations, thirty days from their saved mtime."""
    rows = conn.execute("SELECT f.prediction_id FROM prediction_provenance f JOIN predictions p ON p.id=f.prediction_id "
        "WHERE f.state='ready' AND p.predicted_at<? AND NOT EXISTS(SELECT 1 FROM recording_vibes r WHERE r.prediction_id=p.id) "
        "ORDER BY p.predicted_at,p.id LIMIT ?", (now-30*86400, limit)).fetchall()
    for pid, in rows:
        conn.execute("UPDATE prediction_provenance SET state='expired' WHERE prediction_id=?", (pid,))
    for group in sorted({pid // CHUNK_ROWS for pid, in rows}):
        pid = group * CHUNK_ROWS + 1
        records = _chunk(conn, pid)
        ready = {str(r[0]) for r in conn.execute("SELECT prediction_id FROM prediction_provenance "
            "WHERE state='ready' AND prediction_id>=? AND prediction_id<?", (group*CHUNK_ROWS, (group+1)*CHUNK_ROWS))}
        retained = {key: value for key, value in records.items() if key in ready}
        path = directory(conn) / relative(pid)
        if retained:
            fao.write_bytes(path, canonical({'format': 1, 'calculations': retained}), max_bytes=MAX_BYTES)
        else:
            fao.delete(path, missing_ok=True)
    return len(rows)
