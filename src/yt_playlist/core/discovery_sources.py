"""Typed discovery evidence. Callers own transactions and the Store lock."""


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
