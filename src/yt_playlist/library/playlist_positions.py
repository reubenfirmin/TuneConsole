"""Durable placement of newly added alternates, retried independently of the add itself."""
import json
import logging
from contextlib import ExitStack

from yt_playlist.library.action_claim import claim_deletion_target
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.playlist_policy import placement_moves
from yt_playlist.util.action_kinds import ADD_TRACKS
from yt_playlist.util.retry import with_retry

logger = logging.getLogger(__name__)


def pending_insertions(store, playlist_id):
    pending = []
    for action in store.actions.get_pending_placements(playlist_id):
        plan = json.loads(action.plan_json)
        if plan.get("playlist_id") == playlist_id:
            pending.append((action, plan))
    return pending


def _move(client, ytm_id, plan):
    tracks = validate_playlist(client.get_playlist(ytm_id, limit=None))["tracks"]
    for move in placement_moves(tracks, plan):
        result = client.edit_playlist(ytm_id, moveItem=move)
        status = result.get("status") if isinstance(result, dict) else result
        if status != "STATUS_SUCCEEDED":
            raise ValueError("YouTube did not confirm the playlist move")


def _retain_local_order(store, playlist_id, plan, now):
    ids = store.playlists.get_playlist_track_ids(playlist_id)
    tmap = store.tracks.track_ids_for_videos([plan["after_video_id"], *plan["video_ids"]])
    anchor = tmap.get(plan["after_video_id"])
    if anchor not in ids:
        return
    added = [tmap[v] for v in plan["video_ids"] if v in tmap]
    ids = [tid for tid in ids if tid not in added]
    index = ids.index(anchor) + 1
    combined = ids[:index] + added + ids[index:]
    store.playlists.set_playlist_tracks(playlist_id, combined)
    store.playlists.set_playlist_track_count(playlist_id, len(combined), now)


def retry_insertions(store, playlist_id, client, now, *, attempts=3):
    """Retry moves only; never add the tracks again. Pending actions survive process restarts."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        return 0
    remaining = 0
    for action, plan in pending_insertions(store, playlist_id):
        current = store.actions.get_action(action.id)
        if current is None or current.status != "pending":
            continue

        def move_if_pending():
            current = store.actions.get_action(action.id)
            if current is not None and current.status == "pending":
                _move(client, pl.provider_playlist_id, plan)

        claims = []
        try:
            with ExitStack() as stack:
                stack.enter_context(claim_deletion_target(store,pl.account_id,pl.provider_playlist_id))
                store.actions.require_known_remote_outcome(pl.account_id,pl.provider_playlist_id)
                claims = store.actions.claim_forward_targets(action.id,'placement',now)
                for claim in claims:
                    stack.callback(store.actions.release_deletion,claim)
                with_retry(move_if_pending, attempts=attempts,
                           base_delay=0.1, operation=f"place alternate versions in {pl.title!r}")
                with store._lock:
                    current = store.actions.get_action(action.id)
                    if current is None or current.status!='pending' or current.revision!=action.revision:
                        continue
                    store.actions.update_action(action.id,'executed',now,expected_revision=action.revision,target_claims=claims)
                    _retain_local_order(store,playlist_id,plan,now)
        except Exception:  # noqa: BLE001 - the add succeeded; retain the unfinished placement
            with store._lock:
                current = store.actions.get_action(action.id)
                if current is None or current.status != "pending":
                    continue
                remaining += 1
                logger.warning("alternate placement remains pending for playlist %s (action %s)",
                               playlist_id, action.id, exc_info=True)
                _retain_local_order(store, playlist_id, plan, now)
    return remaining


def cancel_insertions(store, playlist_id, now):
    """A later explicit remove/reorder supersedes the earlier insertion request."""
    for action, _ in pending_insertions(store, playlist_id):
        store.actions.update_action(action.id, "cancelled", now)
