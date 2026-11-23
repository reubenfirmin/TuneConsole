"""Dated confirmed rating changes on the existing player event stream."""
from . import worker_settings


VIEW = 'confirmed_rating_actions'
KIND = 'confirmed_rating'
SERIAL = 'confirmed_rating_action_id_high_water'


def append(conn, rows, *, playlist=None, capture_id=None):
    rows = tuple(rows)
    if not rows:
        return
    last = conn.execute('SELECT max(rating_action_id) FROM player_events WHERE rating_action_id IS NOT NULL').fetchone()[0]
    high_water = max(last or 0, int(worker_settings.read(conn, SERIAL, '0')))
    conn.executemany("""INSERT INTO player_events(account_id,kind,video_id,playlist_ytm_id,payload,at,rating_action_id)
        VALUES (?, ?, ?, ?, json_object('previous_status',?,'status',?,'source',?,'capture_id',?), ?, ?)""",
        ((account, KIND, video, playlist, previous, status, source, capture_id, at, identity)
         for identity, (account, video, previous, status, source, at) in enumerate(rows, high_water + 1)))
    worker_settings.save(conn, SERIAL, str(high_water + len(rows)))
