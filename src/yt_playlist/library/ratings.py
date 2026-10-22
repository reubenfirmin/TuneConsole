"""Explicit song ratings, confirmed by YouTube before updating local state."""
import logging

from yt_playlist.rec import graduation
from yt_playlist.util.retry import with_retry


STATUSES = {"LIKE", "DISLIKE", "INDIFFERENT"}
logger = logging.getLogger(__name__)


def record_confirmed(store, identity_id, video_id, status, now):
    # Keep confirmed API results distinct from unconfirmed player-bar readouts.
    store.tracks.record_item_rating(identity_id, video_id, status, source="api", now=now)
    store.playlists.set_song_liked(identity_id, video_id, status == "LIKE")
    key = store.tracks.identity_key_for_video(video_id)
    if key:
        graduation.apply_dislikes(store, {key: status}, now, provenance="action")


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
        if status == "INDIFFERENT" and not store.tracks.identity_key_for_video(video_id):
            continue
        key = _resolve_track(store, client, video_id, now)
        if key:
            # A newer rating may have arrived while metadata was fetched.
            with store._lock:
                current = store.tracks.item_rating(identity_id, video_id)
                if current is None or current["observed_at"] != now or current["status"] != status:
                    continue
                store.playlists.set_song_liked(identity_id, video_id, status == "LIKE")
                graduation.apply_dislikes(store, {key: status}, now, provenance="action")
            keys.add(key)
    return keys


def set_rating(store, identity_id, video_id, status, client, now):
    if not isinstance(video_id, str) or not video_id.strip():
        raise ValueError("A video ID is required")
    if status not in STATUSES:
        raise ValueError("Invalid rating state")
    result = with_retry(lambda: client.rate_song(video_id, status))
    if isinstance(result, dict) and result.get("status", "STATUS_SUCCEEDED") != "STATUS_SUCCEEDED":
        raise ValueError("YouTube wouldn’t update this song’s rating.")
    record_confirmed(store, identity_id, video_id, status, now)
    if not store.tracks.identity_key_for_video(video_id) and status != "INDIFFERENT":
        reconcile_pending(store, identity_id, client)


def main_rating(store, video_id):
    try:
        identity = store.identities.get_master_identity()
    except ValueError:
        return None
    return store.tracks.item_rating(identity.id, video_id)
