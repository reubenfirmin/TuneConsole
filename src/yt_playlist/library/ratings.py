"""Explicit song ratings, confirmed by YouTube before updating local state."""
import logging

from yt_playlist.rec import graduation
from yt_playlist.library.rating_writes import ordered
from yt_playlist.util.retry import with_retry


STATUSES = {"LIKE", "DISLIKE", "INDIFFERENT"}
logger = logging.getLogger(__name__)


def record_confirmed(store, identity_id, video_id, status, now):
    # Keep confirmed API results distinct from unconfirmed player-bar readouts.
    with store._lock:
        record_observations(store, identity_id, {video_id: status}, source="api", now=now)


def reconcile_recordings(store, keys):
    """One owner for feedback shared by accounts and verified alternate uploads."""
    with store._lock:
        graduation.apply_ratings(store, store.tracks.recording_ratings(keys))


def record_observations(store, identity_id, statuses, *, source, now):
    """Persist trustworthy exact-item evidence, then project the retained contributions together.

    Incomplete sync callers omit neutral observations. A missing account/item contributes
    its retained evidence, never an invented neutral. Sync confirmations of a live action
    preserve that action's provenance and original timestamp.
    """
    with store._lock:
        store.tracks.record_item_ratings(identity_id, statuses, source=source, now=now)
        retained = store.tracks.retained_rating_statuses(identity_id, statuses,
            newer_than=now if source == 'sync' else None)
        # A stale LM snapshot can also omit a newly liked upload. Restore actions at/after
        # its read time, even when that upload was absent from all observed playlist contexts.
        for video_id, status in retained.items():
            store.playlists.set_song_liked(identity_id, video_id, status == 'LIKE')
        reconcile_recordings(store, {store.tracks.identity_key_for_video(v) for v in retained})


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


def reconcile_pending(store, identity_id, client):
    """Resolve live ratings outside the bridge receive loop; failed reads remain retryable.

    Revisit known subjects too: a playlist/history scan may have resolved an upload since its
    rating arrived. Graduation is once per recording and uses the original action timestamp.
    """
    keys = set()
    for row in store.tracks.pending_action_ratings(identity_id):
        video_id, status, now = row["video_id"], row["status"], row["observed_at"]
        key = _resolve_track(store, client, video_id, now)
        if key:
            # A newer rating may have arrived while metadata was fetched.
            with store._lock:
                current = store.tracks.item_rating(identity_id, video_id)
                if current is None or current["observed_at"] != now or current["status"] != status:
                    continue
                store.playlists.set_song_liked(identity_id, video_id, status == "LIKE")
                reconcile_recordings(store, {key})
            keys.add(key)
    return keys


def set_rating(store, identity_id, video_id, status, client, now, *, after_confirmed=None):
    if not isinstance(video_id, str) or not video_id.strip():
        raise ValueError("A video ID is required")
    if status not in STATUSES:
        raise ValueError("Invalid rating state")
    with ordered(store, identity_id, video_id):
        result = with_retry(lambda: client.rate_song(video_id, status))
        if isinstance(result, dict) and result.get("status", "STATUS_SUCCEEDED") != "STATUS_SUCCEEDED":
            raise ValueError("YouTube wouldn’t update this song’s rating.")
        with store._lock:
            # An earlier command's native confirmation may have arrived while this intent waited.
            # Publish this later, confirmed command after it even with a coarse/frozen caller clock.
            retained = store.tracks.item_rating(identity_id, video_id)
            confirmed_at = max(now, retained["observed_at"]) if retained else now
            record_confirmed(store, identity_id, video_id, status, confirmed_at)
        if after_confirmed is not None:
            try:
                return after_confirmed(identity_id)
            except Exception as exc:
                logger.warning("rating saved but player reconciliation failed: video=%s", video_id, exc_info=True)
                return {"player_error": str(exc)}
        return None


def main_rating(store, video_id):
    try:
        identity = store.identities.get_master_identity()
    except ValueError:
        return None
    return store.tracks.item_rating(identity.id, video_id)
