"""Typed discovery evidence. Callers own transactions and the Store lock."""
import json


def source_identity(token):
    for kind in ('radio', 'genre'):
        if token.startswith(kind + ':'):
            return kind, token[len(kind) + 1:]
    return 'album', token


def retain(conn, recording_id, token, video_id, now, *, first_found_at=None):
    kind, identifier = source_identity(token)
    conn.execute(
        "INSERT INTO discovery_sources(recording_id,source_token,input_video_id,found_at,"
        "source_type,source_id,last_found_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(recording_id,source_token) DO UPDATE SET "
        "found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)), "
        "last_found_at=MAX(COALESCE(last_found_at,excluded.last_found_at),"
        "COALESCE(excluded.last_found_at,last_found_at))",
        (recording_id, token, video_id, now if first_found_at is None else first_found_at,
         kind, identifier, now))


def merge_membership(conn, recording_id, member):
    conn.execute(
        'INSERT INTO discovery_memberships VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(recording_id) DO UPDATE SET '
        'found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)), '
        'offered_count=offered_count+excluded.offered_count, '
        'first_shown=MIN(COALESCE(first_shown,excluded.first_shown),COALESCE(excluded.first_shown,first_shown)), '
        'last_shown=MAX(COALESCE(last_shown,excluded.last_shown),COALESCE(excluded.last_shown,last_shown))',
        (recording_id,member['entry_id'],member['found_at'],member['first_shown'],
         member['last_shown'],member['offered_count']))


def prepare_reassociation(conn, video_id, previous):
    """Release a representative before moving its entry; preserve recording-level counters."""
    member = conn.execute('SELECT * FROM discovery_memberships WHERE recording_id=?', (previous,)).fetchone()
    remaining = conn.execute('SELECT MIN(id) FROM catalog_entries WHERE recording_id=? AND video_id IS NOT ?',
                             (previous,video_id)).fetchone()[0]
    if member:
        if remaining is None:
            conn.execute('DELETE FROM discovery_memberships WHERE recording_id=?', (previous,))
        elif conn.execute('SELECT video_id FROM catalog_entries WHERE id=?', (member['entry_id'],)).fetchone()[0] == video_id:
            conn.execute('UPDATE discovery_memberships SET entry_id=? WHERE recording_id=?', (remaining,previous))
    return member, remaining


def finish_reassociation(conn, video_id, previous, target, member, remaining, now, *, association_id):
    """Exact source evidence follows its upload; a split never copies aggregate offer counts."""
    sources = conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? '
                           + ('' if remaining is None else 'AND input_video_id=?'),
                           (previous,) if remaining is None else (previous,video_id)).fetchall()
    if member and remaining is None:
        merge_membership(conn,target,member)
    elif member and sources:
        entry = conn.execute('SELECT id FROM catalog_entries WHERE video_id=?',(video_id,)).fetchone()[0]
        found = [row['found_at'] for row in sources if row['found_at'] is not None]
        conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
                     'ON CONFLICT(recording_id) DO NOTHING', (target,entry,min(found) if found else now))
    for row in sources:
        # Collision aggregation retains the original inputs, including which upload
        # supplied each source. This evidence must survive subsequent pool expiry.
        for original in [row,conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? AND source_token=?',
                                         (target,row['source_token'])).fetchone()]:
            if original is not None:
                conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                             ('discovery_reassociation',json.dumps([association_id,original['recording_id'],original['source_token']]),
                              json.dumps(dict(original)),'exact upload reassociation; retained pre-merge evidence'))
        retain(conn,target,row['source_token'],row['input_video_id'],row['last_found_at'],
               first_found_at=row['found_at'])
        conn.execute('DELETE FROM discovery_sources WHERE recording_id=? AND source_token=?',
                     (previous,row['source_token']))
