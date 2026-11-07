"""Mood event membership. Callers own the transaction and Store lock."""


def _legacy_journal(conn):
    # Explicit historical replay can still be before v66. Its original JSON
    # journal has no unresolved-membership table; v66 resolves those retained
    # keys after the catalog aliases/redirects have been installed.
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='rec_mood'").fetchone() is not None


def resolve_seed(conn, key):
    direct = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    candidates = [direct[0]] if direct else [row[0] for row in conn.execute(
        'SELECT recording_id FROM recording_aliases WHERE alias=?', (key,))]
    resolved = set()
    for recording_id in candidates:
        seen = set()
        while recording_id not in seen:
            seen.add(recording_id)
            target = conn.execute('SELECT recording_id FROM recording_redirects WHERE retired_id=?',
                                  (recording_id,)).fetchone()
            if target is None:
                resolved.add(recording_id)
                break
            recording_id = target[0]
        else:
            raise ValueError('Cyclic recording redirect in mood seed')
    if len(resolved) == 1:
        return resolved.pop(), None
    return None, 'ambiguous_alias' if resolved else 'unknown_recording'


def _add_seed(conn, event_id, recording_id, key, position, reason=None):
    if recording_id is None:
        conn.execute('INSERT INTO mood_event_unresolved_seeds VALUES (?,?,?,?)',
                     (event_id, key, position, reason))
    else:
        conn.execute('INSERT INTO mood_event_recordings VALUES (?,?,?) '
                     'ON CONFLICT(mood_event_id,recording_id) DO UPDATE '
                     'SET position=min(position,excluded.position)',
                     (event_id, recording_id, position))


def record(conn, keys, direction, now, *, cap=None, prune_before=None):
    seeds = []
    seen = set()
    for position, key in enumerate(keys):
        if not isinstance(key, str):
            raise ValueError('Mood seed must be a recording lookup key')
        recording_id, reason = resolve_seed(conn, key)
        identity = ('recording', recording_id) if recording_id is not None else ('unresolved', key)
        if identity not in seen:
            seeds.append((recording_id, key, position, reason))
            seen.add(identity)
    event_id = conn.execute('INSERT INTO mood_events(created_at,direction) VALUES (?,?)',
                            (now, int(direction))).lastrowid
    for recording_id, key, position, reason in seeds:
        _add_seed(conn, event_id, recording_id, key, position, reason)
    if prune_before is not None:
        conn.execute('DELETE FROM mood_events WHERE created_at<?', (prune_before,))
    if cap is not None:
        conn.execute('DELETE FROM mood_events WHERE id NOT IN '
                     '(SELECT id FROM mood_events ORDER BY created_at DESC,id DESC LIMIT ?)', (cap,))
    return event_id


def recent(conn, limit):
    events = list(conn.execute('SELECT id,created_at,direction FROM mood_events '
                               'ORDER BY created_at DESC,id DESC LIMIT ?', (limit,)))
    if not events:
        return []
    ids = [row[0] for row in events]
    placeholders = ','.join('?' for _ in ids)
    seeds = {event_id: [] for event_id in ids}
    for event_id, position, key in conn.execute(
            'SELECT s.mood_event_id,s.position,r.identity_key FROM mood_event_recordings s '
            f'JOIN recordings r ON r.id=s.recording_id WHERE s.mood_event_id IN ({placeholders}) '
            'UNION ALL SELECT mood_event_id,position,legacy_key FROM mood_event_unresolved_seeds '
            f'WHERE mood_event_id IN ({placeholders}) ORDER BY 1,2', ids + ids):
        seeds[event_id].append(key)
    return [(created_at, direction, seeds[event_id]) for event_id, created_at, direction in events]


def resolve_pending(conn, key):
    if _legacy_journal(conn):
        return
    seeds = list(conn.execute('SELECT mood_event_id,position FROM mood_event_unresolved_seeds '
                              'WHERE legacy_key=?', (key,)))
    if not seeds:
        return
    recording_id, reason = resolve_seed(conn, key)
    if recording_id is None:
        conn.execute('UPDATE mood_event_unresolved_seeds SET reason=? WHERE legacy_key=?', (reason, key))
        return
    conn.execute('DELETE FROM mood_event_unresolved_seeds WHERE legacy_key=?', (key,))
    for event_id, position in seeds:
        _add_seed(conn, event_id, recording_id, key, position)


def reassociate(conn, old_key, recording_id, *, previous_recording_id=None):
    if _legacy_journal(conn):
        return
    # A pending key can still resolve to the previous owner until the catalog
    # publishes its redirect. Resolve it before moving that owner's membership.
    resolve_pending(conn, old_key)
    if previous_recording_id is not None and previous_recording_id != recording_id:
        seeds = list(conn.execute('SELECT mood_event_id,position FROM mood_event_recordings '
                                  'WHERE recording_id=?', (previous_recording_id,)))
        conn.execute('DELETE FROM mood_event_recordings WHERE recording_id=?', (previous_recording_id,))
        for event_id, position in seeds:
            _add_seed(conn, event_id, recording_id, old_key, position)
