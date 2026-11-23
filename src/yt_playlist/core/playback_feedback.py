"""Replay durable playback observations through their local domain projectors."""
import logging

from yt_playlist.core.playback_inbox import capture, deferred, inbox
from yt_playlist.core.work_errors import WorkBudgetExceeded

log = logging.getLogger(__name__)


def record_next(store, identity, video, position, duration, playlist, now, *, rating_advance=False):
    body = {'kind': 'next', 'videoId': video, 'position': position,
            'duration': duration, 'playlist': playlist}
    if rating_advance:
        body['ratingAdvance'] = True
    capture(store, 'player', identity, body, now)
    try_drain(store)


def _prepare(store, row):
    """Admit catalog inputs, whose retained metadata requires its own commits."""
    body, now = row['body'], row['observed_at']
    if row['kind'] == 'live':
        from yt_playlist.library.playback_composition import observations
        return {**row, 'live_subject': observations(store).live_subject(body, now)}
    if row['kind'] == 'rating' and (acquisition := body.get('acquisition')) is not None:
        store.tracks.upsert_track(body['video_id'], acquisition['title'], acquisition['artist'],
                                  acquisition.get('album'), None, created_at=now)
    return row


def _apply(store, row):
    """Project one observation before the controller records its receipt."""
    from yt_playlist.library import rating_projection
    body, identity, now = row['body'], row['account_id'], row['observed_at']
    if row['kind'] == 'rating':
        acquisition = body.get('acquisition')
        context = body.get('playback') or {}
        rating_projection.record_confirmed(store, identity, body['video_id'], body['status'], now,
                                           playlist=context.get('playlist'), capture_id=row['capture_id'])
        if body['status'] == 'LIKE':
            from yt_playlist.rec import measurement
            measurement.record_action(store, 'save', now, video_id=body['video_id'],
                keys=[acquisition['identity_key']] if acquisition else (), event_id=row['capture_id'])
        return True
    from yt_playlist.controllers.playback_observations import Observation
    from yt_playlist.library.playback_composition import observations
    observation = Observation(row['kind'], identity, body, now)
    projector = observations(store, in_projection=True)
    if row['kind'] == 'player':
        return projector.project(observation, capture_id=row['capture_id'], attribute=False)
    return projector.project_live(observation, row['live_subject'], inline_analytics=True, attribute=False)


def _attribute(store, row, result):
    """Publish choice evidence from committed events using stable action IDs."""
    from yt_playlist.controllers.playback_observations import player_projection
    from yt_playlist.library.choice_composition import playback
    body, account, now = row['body'], row['account_id'], row['observed_at']
    if row['kind'] == 'rating' and body['status'] == 'LIKE' and (context := body.get('playback')) is not None:
        playback(store, synchronous=True).save_rating(account, context['playlist'], body['video_id'],
                                                      row['capture_id'], context['at'], now)
    elif row['kind'] == 'live' and result:
        playback(store, synchronous=True).record(body, now, account_id=account)
    elif row['kind'] == 'player':
        event = player_projection(body)
        if event['video'] and (event['exit'] or (result and event['rating'] == 'LIKE')):
            event_id = store.player_events.event_for_capture(account, now, row['capture_id'])
            if event_id is not None:
                choices = playback(store, synchronous=True)
                if event['exit']:
                    choices.observe(account, event['playlist'], event['video'], event['kind'],
                                    event['position'], event['duration'], event_id, now)
                else:
                    choices.observe_like(account, event['playlist'], event['video'], event_id,
                                         event['payload'].get('requestedAt'), now)


def drain(store, *, limit=32):
    """Replay a bounded prefix without waiting for the serving writer or another replay."""
    repository = store.playback_feedback
    with repository.replaying() as admitted:
        if not admitted:
            return 0
        completed = 0
        for _ in range(limit):
            if store.activity_paused:
                break
            row = repository.next_observation()
            if row is None:
                break
            result = repository.apply_observation(row, _prepare, _apply)
            if result is None or store.activity_paused:
                break
            try:
                with store._lock.nonblocking_admission():
                    _attribute(store, row, result)
            except WorkBudgetExceeded:
                break
            if store.activity_paused:
                break
            # A retained receipt makes a failed acknowledgement safe to retry.
            repository.acknowledge_observation(row['capture_id'])
            completed += 1
        return completed


def try_drain(store):
    """Remote confirmation remains valid if local projection must retry."""
    try:
        return drain(store)
    except Exception:
        log.warning('Retained playback feedback awaits local replay', exc_info=True)
        return 0
