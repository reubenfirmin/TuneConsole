"""Retained taste controls and comparisons, separate from disposable preparation."""
import json
import uuid

from . import catalog, genre_identity, taste_axes, worker_settings

from .taste_settings import CHECKPOINT, BASELINE, STATE_KEYS
KEYS = frozenset(STATE_KEYS)
REFERENCE_FIELDS = ('recovery_filename', 'recovery_digest', 'recovery_bytes')


def document(conn, key):
    if key == CHECKPOINT:
        facts = checkpoint_facts(conn)
        if facts['header'] is None:
            return None
        return {'header': facts['header'], 'weights': [
            {'axis': weight['captured_axis'], 'weight': weight['weight'], 'updated_at': weight['updated_at']}
            for weight in facts['weights']
        ]}
    raw = worker_settings.read(conn, STATE_KEYS[key])
    if raw is None:
        return None
    header = json.loads(raw)
    entries = header.pop('entries')
    keys = conn.execute('''SELECT COALESCE(c.identity_key,r.identity_key,e.value->>'unresolved_key')
        FROM json_each(?,'$.entries') e LEFT JOIN recordings r ON r.id=e.value->>'recording_id'
        LEFT JOIN recordings c ON c.id=r.redirect_recording_id ORDER BY e.key''', (raw,))
    for entry, (current_key,) in zip(entries, keys, strict=True):
        entry['current_key'] = current_key
    return {'header': header, 'entries': entries}


def restore(key, captured, value):
    if key == CHECKPOINT:
        return json.dumps({'saved_at': captured['header']['saved_at'], 'params': value, 'weights': captured['weights']})
    rows = captured['entries']
    if len(value)!=len(rows):
        raise ValueError('Taste comparison traces do not match their ordered owner rows')
    return json.dumps([{'key':r['current_key'],'rank':r['rank'],'score':r['score'],'lane':r['lane'],
                       'lane_weight':r['lane_weight'],'trace':value[index]} for index,r in enumerate(rows)])


def prepare(key, raw):
    value = json.loads(raw) if raw else None
    kind = 'taste-checkpoint' if key == CHECKPOINT else 'taste-comparison'
    if value is None:
        return None, kind, None
    if key == CHECKPOINT:
        if (not isinstance(value,dict) or set(value)!={'saved_at','params','weights'}
                or not isinstance(value['params'],dict) or not isinstance(value['weights'],list)
                or any(not isinstance(k,str) or not isinstance(v,str) for k,v in value['params'].items())):
            raise ValueError('Unsupported taste settings checkpoint')
        for weight in value['weights']:
            if not isinstance(weight,dict) or set(weight)!={'axis','weight','updated_at'}:
                raise ValueError('Unsupported checkpoint weight')
        return value, kind, value['params']
    if not isinstance(value,list):
        raise ValueError('Taste comparison baseline must be an ordered list')
    for item in value:
        if not isinstance(item,dict) or set(item)!={'key','rank','score','trace','lane','lane_weight'}:
            raise ValueError('Unsupported taste comparison entry')
        if not isinstance(item['key'],str) or not item['key']:
            raise ValueError('Comparison entry requires its captured identity')
    return value, kind, [item['trace'] for item in value]


def register(conn, key, value, reference):
    if value is None:
        worker_settings.save(conn, STATE_KEYS[key], None)
        return
    if key == CHECKPOINT:
        save_checkpoint(conn, uuid.uuid4().hex, value['saved_at'], reference, value['weights'])
        return
    entries = []
    for position, item in enumerate(value):
        key_now = catalog.resolve_key(conn, item['key'])
        recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key_now,)).fetchone() if key_now else None
        if recording is None:
            raise ValueError('Comparison entry requires a known recording')
        rank = item['rank']
        if rank is not None and (type(rank) is not int or rank <= 0):
            raise ValueError('Comparison rank must be a positive integer')
        if not isinstance(item['lane'], str):
            raise ValueError('Comparison entry requires a lane')
        entries.append(dict(position=position, recording_id=recording[0],
            unresolved_key=None, captured_key=item['key'], rank=rank,
            score=float(item['score']), lane=item['lane'],
            lane_weight=None if item['lane_weight'] is None else float(item['lane_weight'])))
    header = dict(id=1, revision=uuid.uuid4().hex, entries=entries)
    save(conn, key, header, reference)


def save_checkpoint(conn, revision, saved_at, reference, weights):
    captured, seen = [], set()
    for position, row in enumerate(weights):
        axis = taste_axes.identity(conn, row['axis'], create=True)
        if axis in seen:
            raise ValueError('A taste checkpoint must capture each axis once')
        seen.add(axis)
        captured.append(dict(position=position, axis_id=axis, captured_axis=row['axis'], weight=float(row['weight']),
                             updated_at=None if row['updated_at'] is None else float(row['updated_at'])))
    header = dict(id=1, revision=revision, saved_at=float(saved_at), weights=captured)
    save(conn, CHECKPOINT, header, reference)


def save(conn, key, header, reference):
    fields = dict(zip(REFERENCE_FIELDS, reference, strict=True))
    worker_settings.save(conn, STATE_KEYS[key], json.dumps({**header, **fields}, allow_nan=False))


def checkpoint_facts(conn):
    raw = worker_settings.read(conn, STATE_KEYS[CHECKPOINT])
    header = json.loads(raw) if raw is not None else None
    return {'header': header, 'weights': header.pop('weights') if header else []}


def checkpoint_axes(conn, positions):
    """Restore captured identities; alias edits cannot reinterpret old checkpoints."""
    selected = set(positions)
    result = []
    raw = worker_settings.read(conn, STATE_KEYS[CHECKPOINT])
    rows = conn.execute('''SELECT w.value,a.kind,a.genre_key FROM json_each(?,'$.weights') w
        JOIN taste_axes a ON a.id=w.value->>'axis_id' ORDER BY w.key''', (raw,)).fetchall()
    for encoded, kind, genre in rows:
        row = json.loads(encoded)
        if row['position'] not in selected:
            continue
        axis = row['axis_id']
        if kind == 'genre':
            axis = taste_axes.identity(conn, 'genre:' + genre_identity.current_key(conn, genre), create=True)
        result.append((axis, row['weight'], row['updated_at']))
    return result
