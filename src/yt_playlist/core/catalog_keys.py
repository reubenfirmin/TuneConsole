"""Resolve existing recording identities without mutating catalog state."""


def resolve_recording(conn, recording_id):
    seen = set()
    while recording_id not in seen:
        seen.add(recording_id)
        row = conn.execute('SELECT recording_id FROM recording_redirects WHERE retired_id=?', (recording_id,)).fetchone()
        if row is None:
            return recording_id
        recording_id = row[0]
    raise ValueError('Cyclic recording redirects')


def resolve_key(conn, key, video_id=None):
    if video_id and video_id.strip():
        row = conn.execute('SELECT r.identity_key FROM youtube_items y JOIN recordings r ON r.id=y.recording_id WHERE y.video_id=?', (video_id,)).fetchone()
        return row[0] if row else None
    row = conn.execute('SELECT id,identity_key FROM recordings WHERE identity_key=?', (key,)).fetchone()
    if row:
        rid = resolve_recording(conn, row[0])
        return conn.execute('SELECT identity_key FROM recordings WHERE id=?', (rid,)).fetchone()[0]
    rows = conn.execute('SELECT DISTINCT r.identity_key FROM recording_aliases a JOIN recordings r ON r.id=a.recording_id WHERE a.alias=?', (key,)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def play_key(conn, key, video_id):
    resolved = resolve_key(conn, key, video_id)
    if resolved:
        return resolved
    if video_id and video_id.strip() and conn.execute('SELECT 1 FROM recordings WHERE identity_key=?', (key,)).fetchone():
        return '@unresolved-video:' + video_id
    return key
