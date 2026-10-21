"""Durable placement of newly added alternates, retried independently of the add itself."""
import json
import logging

from yt_playlist.util.action_kinds import ADD_TRACKS
from yt_playlist.util.retry import with_retry

logger = logging.getLogger(__name__)


def pending_insertions(store, playlist_id):
    pending = []
    for action in store.actions.get_pending_actions(ADD_TRACKS):
        plan = json.loads(action.plan_json)
        if plan.get("playlist_id") == playlist_id:
            pending.append((action, plan))
    return pending


def _move(client, ytm_id, plan):
    tracks = client.get_playlist(ytm_id, limit=None).get("tracks", [])
    videos = plan["video_ids"]
    # Resolve the anchor's current successor, excluding the songs we are positioning. This also
    # makes a retry safe after only part of a multi-song move succeeded or the old successor went away.
    order = [t["videoId"] for t in tracks if t.get("videoId") and t["videoId"] not in videos]
    anchor = plan["after_video_id"]
    if anchor not in order:
        raise ValueError("the original track is not visible on YouTube yet")
    index = order.index(anchor) + 1
    if index == len(order):
        return  # Appending already placed these songs after the last track.
    successor = order[index]
    handles = {t["videoId"]: t["setVideoId"] for t in tracks if t.get("videoId") and t.get("setVideoId")}
    # YouTube returns the new item handles with the add, before playlist readback indexes them.
    # Prefer those exact handles over a potentially older occurrence of the same video.
    handles.update(plan.get("set_video_ids", {}))
    if any(not handles.get(vid) for vid in [successor, *videos]):
        raise ValueError("YouTube has not exposed the playlist item handles yet")
    for video in videos:
        result = client.edit_playlist(ytm_id, moveItem=(handles[video], handles[successor]))
        status = result.get("status") if isinstance(result, dict) else result
        if status != "STATUS_SUCCEEDED":
            raise ValueError("YouTube did not confirm the playlist move")


def _retain_local_order(store, playlist_id, plan, now):
    ids = store.get_playlist_track_ids(playlist_id)
    tmap = store.track_ids_for_videos([plan["after_video_id"], *plan["video_ids"]])
    anchor = tmap.get(plan["after_video_id"])
    if anchor not in ids:
        return
    added = [tmap[v] for v in plan["video_ids"] if v in tmap]
    ids = [tid for tid in ids if tid not in added]
    index = ids.index(anchor) + 1
    combined = ids[:index] + added + ids[index:]
    store.set_playlist_tracks(playlist_id, combined)
    store.set_playlist_track_count(playlist_id, len(combined), now)


def retry_insertions(store, playlist_id, client, now, *, attempts=3):
    """Retry moves only; never add the tracks again. Pending actions survive process restarts."""
    pl = store.get_playlist(playlist_id)
    if pl is None:
        return 0
    remaining = 0
    for action, plan in pending_insertions(store, playlist_id):
        try:
            with_retry(lambda: _move(client, pl.ytm_playlist_id, plan), attempts=attempts,
                       base_delay=0.1, operation=f"place alternate versions in {pl.title!r}")
        except Exception:  # noqa: BLE001 - the add succeeded; retain the unfinished placement
            remaining += 1
            logger.warning("alternate placement remains pending for playlist %s (action %s)",
                           playlist_id, action.id, exc_info=True)
        else:
            store.update_action(action.id, "executed", now)
        # Sync can read an old append order, or omit the new items entirely. Preserve the intended
        # local order while showing it as pending, and after a successful remote move as well.
        _retain_local_order(store, playlist_id, plan, now)
    return remaining


def cancel_insertions(store, playlist_id, now):
    """A later explicit remove/reorder supersedes the earlier insertion request."""
    for action, _ in pending_insertions(store, playlist_id):
        store.update_action(action.id, "cancelled", now)
