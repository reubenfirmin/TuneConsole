"""Mood event membership. Callers own the transaction and Store lock."""
import json

from .catalog_keys import resolve_recording


def resolve_seed(conn, key):
    direct = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    candidates = [direct[0]] if direct else [row[0] for row in conn.execute(
        'SELECT recording_id FROM recording_aliases WHERE alias=?', (key,))]
    resolved = {resolve_recording(conn, recording_id) for recording_id in candidates}
    if len(resolved) == 1:
        return resolved.pop(), None
    return None, 'ambiguous_alias' if resolved else 'unknown_recording'


def _seed(position, recording, key, reason):
    return {'position': position, 'recording_id': recording,
            'unresolved_key': key if recording is None else None, 'reason': reason}


def _unique(seeds):
    by_identity = {}
    for seed in seeds:
        by_identity.setdefault((seed['recording_id'], seed['unresolved_key']), seed)
    return list(by_identity.values())


def record(conn, keys, direction, now, *, cap=None, prune_before=None):
    seeds = []
    for position, key in enumerate(keys):
        if not isinstance(key, str):
            raise ValueError('Mood seed must be a recording lookup key')
        recording_id, reason = resolve_seed(conn, key)
        seeds.append(_seed(position, recording_id, key, reason))
    event_id = conn.execute('INSERT INTO mood_events(created_at,direction,seeds) VALUES (?,?,?)',
                            (now, int(direction), json.dumps(_unique(seeds)))).lastrowid
    if prune_before is not None:
        conn.execute('DELETE FROM mood_events WHERE created_at<?', (prune_before,))
    if cap is not None:
        conn.execute('DELETE FROM mood_events WHERE id NOT IN '
                     '(SELECT id FROM mood_events ORDER BY created_at DESC,id DESC LIMIT ?)', (cap,))
    return event_id


def recent(conn, limit):
    events = {}
    for event, created_at, direction, key in conn.execute('''WITH recent AS (
            SELECT * FROM mood_events ORDER BY created_at DESC,id DESC LIMIT ?)
        SELECT e.id,e.created_at,e.direction,coalesce(r.identity_key,s.value->>'unresolved_key')
        FROM recent e LEFT JOIN json_each(e.seeds) s
        LEFT JOIN recordings r ON r.id=s.value->>'recording_id'
        ORDER BY e.created_at DESC,e.id DESC,s.key''', (limit,)):
        _, _, keys = events.setdefault(event, (created_at, direction, []))
        if key is not None:
            keys.append(key)
    return list(events.values())


def _replace(conn, field, previous, recording, key, reason):
    events = conn.execute('SELECT e.id,e.seeds FROM mood_events e WHERE EXISTS('
        'SELECT 1 FROM json_each(e.seeds) s WHERE s.value->>?=?)', (field, previous)).fetchall()
    for event, encoded in events:
        seeds = json.loads(encoded)
        changed = [_seed(seed['position'], recording, key, reason) if seed[field] == previous else seed
                   for seed in seeds]
        if changed != seeds:
            conn.execute('UPDATE mood_events SET seeds=? WHERE id=?', (json.dumps(_unique(changed)), event))


def resolve_key(conn, key, recording, reason):
    _replace(conn, 'unresolved_key', key, recording, key, reason)


def move_recording(conn, previous, recording):
    if previous != recording:
        _replace(conn, 'recording_id', previous, recording, None, None)


def resolve_pending(conn, key):
    recording_id, reason = resolve_seed(conn, key)
    resolve_key(conn, key, recording_id, reason)


def reassociate(conn, old_key, recording_id, *, previous_recording_id=None):
    # A pending key can still resolve to the previous owner until the catalog
    # publishes its redirect. Resolve it before moving that owner's membership.
    resolve_pending(conn, old_key)
    if previous_recording_id is not None:
        move_recording(conn, previous_recording_id, recording_id)
