"""Replay durable playback observations through their local domain projectors."""
import logging
from types import SimpleNamespace

from yt_playlist.core.playback_inbox import Inbox, capture, deferred, inbox

log = logging.getLogger(__name__)


def record_next(store, identity, video, position, duration, playlist, now):
    capture(store, 'player', identity, {'kind': 'next', 'videoId': video, 'position': position,
        'duration': duration, 'playlist': playlist}, now)
    try_drain(store)


def _apply(store, row):
    """Apply one finite observation under the caller's atomic SQLite unit."""
    from yt_playlist.library import live_plays, player_events, rating_projection
    body, identity, now = Inbox.body(row), row['account_id'], row['observed_at']
    if row['kind'] == 'rating':
        acquisition = body.get('acquisition')
        if acquisition is not None:
            store.tracks.upsert_track(body['video_id'], acquisition['title'], acquisition['artist'],
                                      acquisition.get('album'), None, created_at=now)
        rating_projection.record_confirmed(store, identity, body['video_id'], body['status'], now)
        if body['status'] == 'LIKE':
            from yt_playlist.rec import measurement
            measurement.record_action(store, 'save', now, video_id=body['video_id'],
                keys=[acquisition['identity_key']] if acquisition else (), event_id=row['capture_id'])
        return True
    ctx = SimpleNamespace(store=store, rec_worker=None)
    if row['kind'] == 'player':
        return player_events.persist_player_event(ctx, body, now, identity)
    return live_plays.persist_play_event(ctx, body, now, identity, inline_analytics=True)


def drain(store, *, limit=32):
    """Replay a bounded prefix without waiting for the serving writer or another replay."""
    queue = inbox(store)
    if store.activity_paused or not queue.replaying.acquire(blocking=False):
        return 0
    completed = 0
    try:
        for _ in range(limit):
            if store.activity_paused:
                break
            row = queue.next()
            if row is None or not store._lock.acquire(blocking=False):
                break
            try:
                if store.activity_paused:
                    break
                with store.conn.atomic():
                    receipt = store.conn.execute('SELECT 1 FROM playback_feedback_receipts WHERE capture_id=?',
                                                 (row['capture_id'],)).fetchone()
                    if receipt is None:
                        result = _apply(store, row)
                        store.conn.execute('INSERT INTO playback_feedback_receipts VALUES (?,?,?,?,?)',
                            (row['capture_id'], row['account_id'], row['kind'], row['observed_at'], bool(result)))
            finally:
                store._lock.release()
            # A retained receipt makes a failed acknowledgement safe to retry.
            queue.acknowledge(row['capture_id'])
            completed += 1
        return completed
    finally:
        queue.replaying.release()


def try_drain(store):
    """Remote confirmation remains valid if local projection must retry."""
    try:
        return drain(store)
    except Exception:
        log.warning('Retained playback feedback awaits local replay', exc_info=True)
        return 0
