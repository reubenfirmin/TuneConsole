"""Explicit song ratings, confirmed by YouTube before updating local state."""
import logging
from yt_playlist.core import playback_feedback
from yt_playlist.core.action_errors import ActionInputError

from yt_playlist.library import rating_projection
from yt_playlist.library.rating_projection import record_confirmed, reconcile_recordings
from yt_playlist.library.rating_writes import ordered
from yt_playlist.util.retry import with_retry


STATUSES = {"LIKE", "DISLIKE", "INDIFFERENT"}
logger = logging.getLogger(__name__)


def record_observations(store, account_id, statuses, *, source, now):
    """Persist trustworthy exact-item evidence, then project the retained contributions together.

    Incomplete sync callers omit neutral observations. A missing account/item contributes
    its retained evidence, never an invented neutral. Sync confirmations of a live action
    preserve that action's provenance and original timestamp.
    """
    if source == 'sync':
        # A stale/incomplete remote snapshot must not overtake locally retained
        # confirmations. Retry the sync after the bounded playback prefix drains.
        playback_feedback.drain(store)
        if store.playback_feedback.pending():
            raise RuntimeError('Playback feedback is awaiting local replay; retry this sync')
    rating_projection.record_observations(store, account_id, statuses, source=source, now=now)


def _resolve_track(store, client, video_id, now):
    key = store.tracks.identity_key_for_video(video_id)
    if key or client is None:
        return key
    try:
        response = with_retry(lambda: client.get_song(video_id))
        details = response.get("videoDetails", {}) if isinstance(response, dict) else {}
        # Reject a fallback player response for another upload; never guess from the current song.
        title = details.get("title")
        artist = details.get("author")
        if (details.get("videoId") != video_id or not isinstance(title, str) or not title.strip()
                or not isinstance(artist, str) or not artist.strip()):
            return None
        return store.tracks.observe_track(video_id, title, artist, now=now)
    except Exception:
        # The remote rating already succeeded. Keep its exact-video evidence for the next sync.
        logger.warning("rating metadata unavailable: video=%s", video_id, exc_info=True)
        return None


def reconcile_pending(store, account_id, client):
    """Resolve live ratings outside the bridge receive loop; failed reads remain retryable.

    Revisit known subjects too: a playlist/history scan may have resolved an upload since its
    rating arrived. Graduation is once per recording and uses the original action timestamp.
    """
    keys = set()
    for row in store.tracks.pending_action_ratings(account_id):
        video_id, status, now = row["video_id"], row["status"], row["observed_at"]
        key = _resolve_track(store, client, video_id, now)
        if key:
            # A newer rating may have arrived while metadata was fetched.
            with store._lock:
                current = store.tracks.item_rating(account_id, video_id)
                if current is None or current["observed_at"] != now or current["status"] != status:
                    continue
                store.playlists.set_song_liked(account_id, video_id, status == "LIKE")
                reconcile_recordings(store, {key})
            keys.add(key)
    return keys


def set_rating(store, account_id, video_id, status, client, now, *, after_confirmed=None, now_fn=None,
               acquisition=None):
    if not isinstance(video_id, str) or not video_id.strip():
        raise ActionInputError('song_required')
    if status not in STATUSES:
        raise ActionInputError('rating_invalid')
    with ordered(store, account_id, video_id):
        # Resolve the retained clock before sending a remote mutation. Once
        # YouTube confirms, local capture must not depend on the main handle
        # remaining open through a compaction cutover.
        retained = main_item_rating(store, account_id, video_id)
        result = with_retry(lambda: client.rate_song(video_id, status))
        if isinstance(result, dict) and result.get("status", "STATUS_SUCCEEDED") != "STATUS_SUCCEEDED":
            raise ActionInputError('rating_rejected')
        # Capture the exact confirmed result before acknowledging it. This
        # independent inbox never waits for maintenance's serving connection.
        errors = []
        try:
            confirmed_now = now_fn() if now_fn is not None else now
            confirmed_at = max(confirmed_now, retained['observed_at']) if retained else confirmed_now
            body = {'video_id': video_id, 'status': status}
            if acquisition is not None:
                body['acquisition'] = acquisition
            playback_feedback.capture(store, 'rating', account_id, body, confirmed_at)
            playback_feedback.try_drain(store)
        except Exception:
            logger.warning('YouTube confirmed rating but local capture failed: video=%s', video_id, exc_info=True)
            errors.append('YouTube saved the rating, but its local history could not be saved. Check storage and sync again.')
        native = {}
        if after_confirmed is not None:
            try:
                native = after_confirmed(account_id) or {}
            except Exception as exc:
                logger.warning("rating saved but player reconciliation failed: video=%s", video_id, exc_info=True)
                errors.append(str(exc))
        if native.get('player_error'):
            errors.append(native['player_error'])
        return {**native, 'player_error': ' '.join(errors)} if errors else native or None


def main_rating(store, video_id):
    try:
        identity = store.accounts.get_master_account()
    except ValueError:
        return None
    return main_item_rating(store, identity.id, video_id)


def main_item_rating(store, identity, video):
    # The current player thumb may lag an API write. Read retained SQL without
    # waiting for its writer; confirmed queued changes are shown immediately.
    result = store.tracks.item_rating(identity, video)
    queued = store.playback_feedback.queued_rating(identity, video)
    return queued if queued is not None and (result is None or queued['observed_at'] >= result['observed_at']) else result
