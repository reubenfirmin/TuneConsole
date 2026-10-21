"""Bounded playlist additions shared by imports, mutations and recovery orchestration."""
import logging

logger = logging.getLogger(__name__)


def add_items(client, ytm_playlist_id, video_ids, set_video_ids=None):
    """Recover from a rejected batch one item at a time; report added count and skipped IDs."""
    if not video_ids:
        return 0, []
    try:
        result = client.add_playlist_items(ytm_playlist_id, video_ids)
        _require_add_success(result)
        _remember_added_handles(result, set_video_ids)
        return len(video_ids), []
    except Exception:
        logger.warning("batch add of %d items to %s failed; retrying individually",
                       len(video_ids), ytm_playlist_id)
        added, skipped = 0, []
        for v in video_ids:
            try:
                result = client.add_playlist_items(ytm_playlist_id, [v])
                _require_add_success(result)
                _remember_added_handles(result, set_video_ids)
                added += 1
            except Exception:
                skipped.append(v)
        if skipped:
            logger.warning("skipped %d unaddable item(s) for %s", len(skipped), ytm_playlist_id)
    return added, skipped


def _require_add_success(result):
    status = result.get("status") if isinstance(result, dict) else result
    if status != "STATUS_SUCCEEDED":
        raise ValueError("YouTube did not confirm the playlist additions")


def _remember_added_handles(result, handles):
    if handles is None or not isinstance(result, dict):
        return
    for item in result.get("playlistEditResults", []):
        if item and item.get("videoId") and item.get("setVideoId"):
            handles[item["videoId"]] = item["setVideoId"]


def add_items_resilient(client, playlist_id, item_ids, batch_size=100, *, set_video_ids=None):
    """Bounded batches with per-item recovery for a rejected batch."""
    added, skipped = 0, []
    ids = list(item_ids)
    for start in range(0, len(ids), max(1, batch_size)):
        n, bad = add_items(client, playlist_id, ids[start:start + max(1, batch_size)], set_video_ids)
        added += n
        skipped.extend(bad)
    return added, skipped
