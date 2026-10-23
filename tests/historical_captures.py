"""Raw historical capture fixtures for migration and recovery regressions only.

Ordinary behavior fixtures must use dated observations or timestamped events.
Capture timestamps here retain unknown play precision and unknown timezone.
"""


def legacy_capture(conn, identity_id, captured_at, item_keys, *, source="unknown"):
    with conn:
        sid = conn.execute(
            "INSERT INTO history_snapshots(identity_id,taken_at) VALUES (?,?)",
            (identity_id, captured_at)).lastrowid
        conn.executemany(
            "INSERT INTO history_items(snapshot_id,identity_key,source) VALUES (?,?,?)",
            [(sid, key, source) for key in item_keys])
    return sid


def retained_capture(conn, identity_id, captured_at, item_keys, *, source="unknown"):
    """Construct already-converted historical evidence for current repair/recovery tests."""
    from yt_playlist.core import catalog

    with conn:
        capture_id = conn.execute('INSERT INTO listening_capture_receipts(identity_id,captured_at) VALUES (?,?)',
                                  (identity_id, captured_at)).lastrowid
        item_id = conn.execute('SELECT COALESCE(MAX(capture_item_id),0)+1 FROM listening_observations').fetchone()[0]
        for raw in item_keys:
            key = catalog.resolve_key(conn, raw) or raw
            recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            conn.execute(
                'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,recording_id,'
                'source_day,day,source,timezone,precision,first_observed_at,last_observed_at,capture_id,capture_item_id) '
                "VALUES (?,?,?,?,date(?,'unixepoch'),date(?,'unixepoch'),?,NULL,'legacy_unknown',?,?,?,?)",
                (identity_id, raw, key, recording[0] if recording else None, captured_at, captured_at,
                 source, captured_at, captured_at, capture_id, item_id))
            item_id += 1
    return capture_id
