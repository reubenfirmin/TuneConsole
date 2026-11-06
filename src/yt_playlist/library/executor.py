"""Planned merge/copy execution and edits of existing playlists."""
import json
import logging
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, asdict
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.util.matching import fuzzy_ratio, normalize, track_artist
from yt_playlist.util.retry import with_retry
from yt_playlist.library.playlist_backups import backup_playlist
from yt_playlist.library.playlist_catalog import fetch_song_duration
from yt_playlist.library.playlist_positions import cancel_insertions, retry_insertions
from yt_playlist.library.playlist_mutation import (
    apply_playlist_permutation, confirm_playlist_write, same_playlist_items)
from yt_playlist.library.playlist_snapshot import playlist_if_present, validate_playlist
from yt_playlist.library.playlist_deletion import (
    delete_backed_up, deletion_status, pending_deletion, resume_pending_deletion)
from yt_playlist.library.action_claim import claim_action, claim_playlist_deletion, claim_deletion_target
from yt_playlist.library.undo_capture import capture_copy, capture_post as _capture_post
from yt_playlist.library.playlist_metadata import content_hash
from yt_playlist.library.playlist_writes import (
    add_items as _add_items, add_items_resilient, account_additions, AdditionOutcome,
    PlaylistAddUncertain, retain_addition_snapshot)
from yt_playlist.util.action_kinds import (
    PLAN, APPLY_MERGE, MOVE_IDENTITY, DELETE_EMPTY, DELETE_PLAYLIST,
    ADD_TRACKS, REMOVE_TRACK, RENAME_PLAYLIST, REMOVE_LOCAL_PLAYLIST)
from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS

logger = logging.getLogger(__name__)

class _MissingRequiredTracks(ActionInputError):
    """A verified contents conflict, separate from provider diagnostics."""
    def __init__(self, count):
        self.count = count
        super().__init__('merge_missing_required', count=count)

@dataclass
class Resolution:
    identity_key: str
    source_video_id: str | None
    target_video_id: str | None
    method: str  # reuse | search | unresolved

@dataclass
class MergePlan:
    source_playlist_id: int
    target_playlist_id: int
    additions: list[Resolution]
    unresolved: list[Resolution]

@dataclass
class PlannedExec:
    plan: MergePlan
    mode: str
    source_provider_playlist_id: str

def _require_distinct_playlists(source, target):
    if source.playlist_scope_id == target.playlist_scope_id:
        raise ActionInputError('playlist_same')

def serialize_plan(plan, mode, source_provider_playlist_id, source_title=None, target_title=None):
    params = {"source": plan.source_playlist_id, "target": plan.target_playlist_id,
              "mode": mode, "source_ytm": source_provider_playlist_id,
              "source_title": source_title, "target_title": target_title}
    payload = {"additions": [asdict(r) for r in plan.additions],
               "unresolved": [asdict(r) for r in plan.unresolved]}
    return json.dumps(params), json.dumps(payload)

def deserialize_plan(action) -> PlannedExec:
    params = json.loads(action.params_json)
    payload = json.loads(action.plan_json)
    plan = MergePlan(
        params["source"], params["target"],
        [Resolution(**d) for d in payload["additions"]],
        [Resolution(**d) for d in payload["unresolved"]])
    return PlannedExec(plan, params["mode"], params["source_ytm"])

def store_plan(store, plan, mode, source_provider_playlist_id, now) -> int:
    # capture titles now so the action log can name the playlists even after one is pruned
    src = store.playlists.get_playlist(plan.source_playlist_id)
    tgt = store.playlists.get_playlist(plan.target_playlist_id)
    if mode == "delete" and src is not None and tgt is not None:
        _require_distinct_playlists(src, tgt)
    params_json, plan_json = serialize_plan(
        plan, mode, source_provider_playlist_id,
        src.title if src else None, tgt.title if tgt else None)
    return store.actions.record_action(PLAN, params_json, plan_json, "planned", "{}", now)

def _tracks_with_meta(store, playlist_id):
    return store.playlists.get_playlist_tracks_with_meta(playlist_id)

def _resolve_in_target(target_client, key, title, artist, source_vid, source_dur, fuzzy_threshold):
    # Prefer reusing the source videoId; else fuzzy-search the target identity,
    # picking the best candidate (within +/-3s of source duration preferred, then highest score).
    if source_vid:
        return Resolution(key, source_vid, source_vid, "reuse")
    results = with_retry(lambda: target_client.search(f"{title} {artist}", "songs")) or []
    want = normalize(f"{title} {artist}")
    best = None  # (within3s: bool, score: float, video_id)
    for r in results:
        score = fuzzy_ratio(want, normalize(f"{r.get('title','')} {track_artist(r)}"))
        if score < fuzzy_threshold:
            continue
        cand_dur = r.get("duration_seconds")
        within = source_dur is not None and cand_dur is not None and abs(cand_dur - source_dur) <= 3
        cand = (within, score, r.get("videoId"))
        if best is None or (cand[0], cand[1]) > (best[0], best[1]):
            best = cand
    # Only accept a search match we're confident is the SAME recording: either its duration matches
    # (within 3s) or the title/artist is near-exact. A merely-similar title (e.g. a 7-min extended mix
    # for the 3-min original) is left unresolved, so a move won't delete the source for a wrong substitute.
    if best is not None and (best[0] or best[1] >= 0.95):
        return Resolution(key, source_vid, best[2], "search")
    return Resolution(key, source_vid, None, "unresolved")

def _remote_snapshot(client, provider_playlist_id):
    return validate_playlist(with_retry(lambda: client.get_playlist(provider_playlist_id, limit=None)))


def _remote_keys(client, provider_playlist_id):
    """Exact remote upload IDs, or None if the complete contents cannot be established."""
    try:
        detail = _remote_snapshot(client, provider_playlist_id)
    except Exception:  # noqa: BLE001 - deleted playlist or ytmusicapi parse failure
        return None
    return {t["videoId"] for t in detail["tracks"]}

def _remote_video_ids(client, provider_playlist_id):
    """Current remote videoIds (in order) of a playlist, for capturing prior contents before a merge."""
    detail = _remote_snapshot(client, provider_playlist_id)
    return [t["videoId"] for t in detail["tracks"]]

def _verify_and_delete(store, source_playlist_id, source_client, target_client, target_ytm, now,
                       *, retained_video_ids=(), excluded_video_ids=(), backups=None,
                       action_id, locked=False, target_locked=False) -> str:
    source = store.playlists.get_playlist(source_playlist_id)
    if source is None:
        raise ActionInputError('playlist_missing')
    # A second account's saved membership is still the same remote object.
    # Content verification cannot establish an independent surviving copy.
    if source.provider_playlist_id == target_ytm:
        raise ActionInputError('playlist_kept')
    with nullcontext() if target_locked else claim_playlist_deletion(store, source):
        if pending_deletion(store, source) is not None:
            raise ActionInputError('playlist_deletion_pending')
        return _verify_claimed_and_delete(
            store, source_playlist_id, source_client, target_client, target_ytm, now,
            retained_video_ids=retained_video_ids, excluded_video_ids=excluded_video_ids,
            backups=backups, action_id=action_id, locked=locked)


def _verify_claimed_and_delete(store, source_playlist_id, source_client, target_client, target_ytm, now,
                               *, retained_video_ids, excluded_video_ids, backups, action_id, locked):
    """Shared copy-before-delete gate for merges, moves and duplicate removal.

    Compare exact uploads from fresh reads. Only an explicit merge selection may exclude known
    source uploads; tracks added since the last sync must survive too. Never trust cached contents
    or a successful write response as proof that the destination received the required tracks.
    """
    source = store.playlists.get_playlist(source_playlist_id)
    try:
        source_snapshot = _remote_snapshot(source_client, source.provider_playlist_id)
    except Exception as exc:
        raise ValueError("Couldn't read the source playlist from YouTube to verify its contents. "
                         "Refusing to delete. Re-sync and try again.") from exc
    source_keys = {t["videoId"] for t in source_snapshot["tracks"]}
    required = (source_keys - set(excluded_video_ids)) | set(retained_video_ids)
    target_keys = _remote_keys(target_client, target_ytm)
    if target_keys is None:
        raise ValueError("Couldn't read the kept playlist from YouTube to verify its contents. "
                         "Refusing to delete. Re-sync and try again.")
    missing = required - target_keys
    if missing:
        raise _MissingRequiredTracks(len(missing))
    backup_path = backup_playlist(store, source_playlist_id, now, source_client,
                                  snapshot=source_snapshot)
    if backups is not None:
        backups.append(backup_path)  # retain undo evidence even if the delete response is lost
    logger.warning("deleting source playlist %s (backup at %s)", source.provider_playlist_id, backup_path)
    delete_backed_up(store, source, source_client, backup_path, now, kind=PLAN,
                     action_id=action_id, locked=locked)
    return backup_path

def _reconcile(client, provider_playlist_id, desired_video_ids):
    """Reconcile unique membership, retaining the write-constrained append order.

    Returns counts, the complete pre-write baseline, rejected IDs and the sequence
    constrained by those writes. A separate complete read must verify that sequence
    before it becomes Undo evidence.
    """
    detail = _remote_snapshot(client, provider_playlist_id)
    tracks = detail["tracks"]
    prior = [t.get("videoId") for t in tracks if t.get("videoId")]
    desired = list(dict.fromkeys(v for v in desired_video_ids if v))   # de-dupe, keep order
    desired_set = set(desired)
    current = set(prior)
    to_add = [v for v in desired if v not in current]
    to_remove, kept = [], set()
    for track in tracks:
        video = track["videoId"]
        if video not in desired_set or video in kept:
            if video in desired_set and video in kept and not track.get("setVideoId"):
                raise ValueError("YouTube has not exposed the repeated item's removal handle.")
            to_remove.append(track)
        kept.add(video)
    added, skipped = _add_items(client, provider_playlist_id, to_add, prior_tracks=tracks)
    if to_remove:
        client.remove_playlist_items(provider_playlist_id, to_remove)
    expected = list(dict.fromkeys(v for v in prior if v in desired_set)) + [v for v in to_add if v not in skipped]
    return added, len(to_remove), prior, skipped, expected

def apply_result(store, clients, playlist_ids, result_video_ids, keep, now, *, reviewed_draft=None) -> dict:
    """N-way track-level merge: set the kept playlist(s) to exactly result_video_ids.

    keep == "all": set every playlist in playlist_ids to the result (keep them all).
    keep == <playlist id>: set that one to the result and delete the others (each backed up first).
    """
    pls = [store.playlists.get_playlist(pid) for pid in playlist_ids]
    if any(p is None for p in pls):
        raise ActionInputError('playlist_missing')
    if len(pls) < 2:
        raise ActionInputError('merge_unavailable')
    by_scope = {}
    for playlist in pls:
        previous = by_scope.get(playlist.playlist_scope_id)
        if previous is not None:
            _require_distinct_playlists(previous, playlist)
        by_scope[playlist.playlist_scope_id] = playlist

    def client_for(pl):
        c = clients.get(pl.account_id)
        if c is None:
            raise ActionInputError('playlist_account_disconnected')
        return c

    keep_all = (str(keep) == "all")
    try:
        keep_id = None if keep_all else int(keep)
    except (ValueError, TypeError):
        raise ActionInputError('merge_keeper_required') from None
    keepers = pls if keep_all else [p for p in pls if p.id == keep_id]
    if not keepers:
        raise ActionInputError('merge_keeper_required')
    # YouTube auto-manages system playlists (Liked Music, Episodes for Later) and rejects every
    # added track. Merging *into* one would silently add nothing yet still delete the source. Refuse.
    sysk = next((p for p in keepers if p.provider_playlist_id in SYSTEM_PLAYLIST_IDS), None)
    if sysk is not None:
        raise ActionInputError('merge_system_keeper')
    droppers = [] if keep_all else [p for p in pls if p.id != int(keep)]
    result_video_ids = list(dict.fromkeys(v for v in result_video_ids if v))
    retained = set(result_video_ids)

    summary = {"added": 0, "removed": 0, "skipped": 0, "deleted": [],
               "kept_ytm": keepers[0].provider_playlist_id, "kept_title": keepers[0].title}
    # Retain every keeper before mutation and record failures too. Undo evidence
    # stays unknown until an operation-constrained complete read verifies it.
    restored = [{"ytm": pl.provider_playlist_id, "identity": pl.account_id,
                 "prev": _remote_video_ids(client_for(pl), pl.provider_playlist_id),
                 "post_state": None} for pl in keepers]
    # Publish complete keeper backups before any membership or positional write.
    # These stay separate from deleted-source backups: Undo restores each keeper
    # through its conflict-aware keeper step rather than creating another playlist.
    owned = store.actions.has_durable_targets()
    if owned:
        for pl,entry in zip(keepers,restored):
            snapshot = _remote_snapshot(client_for(pl),pl.provider_playlist_id)
            entry['prev'] = [track['videoId'] for track in snapshot['tracks']]
            entry['backup'] = backup_playlist(store,pl.id,now,client_for(pl),snapshot=snapshot)
            entry['phase'] = 'pending'
    backups = []
    action_id = None
    deletion_locks = ExitStack()
    keeper_claims = []

    def _record(extra=None):
        nonlocal action_id
        for entry in restored:
            post = entry["post_state"]
            entry["post"] = post["videos"] if post is not None else None
        params = json.dumps({"kept": summary["kept_title"], "deleted": summary["deleted"],
                                        "added": summary["added"], "removed": summary["removed"],
                                        "skipped": summary["skipped"],
                                        "members": [p.title for p in pls], **(extra or {})})
        undo = {"restored": restored, "backups": backups}
        if action_id is None:
            action_id = store.actions.record_action(
                APPLY_MERGE, params, "{}", "executed", json.dumps(undo), now,
                reviewed_draft=reviewed_draft)
        else:
            action = store.actions.get_action(action_id)
            if action.status not in ("executed", "delete_pending", "delete_failed"):
                return  # an earlier recovery must never be overwritten
            undo.update({"deletions": json.loads(action.undo_json).get("deletions", [])})
            store.actions.update_action(action_id, deletion_status(undo), now,
                                        json.dumps(undo), params_json=params,target_claims=keeper_claims)

    try:
        if owned:
            # Freezing the reviewed draft and committing the backup/intent must
            # finish before the first remote mutation, including keep-all merges.
            _record()
        for pl in sorted(pls,key=lambda item:(item.account_id,item.provider_playlist_id)):
            deletion_locks.enter_context(claim_deletion_target(store,pl.account_id,pl.provider_playlist_id))
            store.actions.require_known_remote_outcome(pl.account_id,pl.provider_playlist_id)
        if owned:
            keeper_claims = store.actions.claim_forward_targets(action_id,'keeper',now)
            for keeper_claim in keeper_claims:
                deletion_locks.callback(store.actions.release_deletion,keeper_claim)
        for pl in keepers:
            # This editor's membership and order replace any earlier alternate placement,
            # including when the new remote mutation later needs recovery.
            cancel_insertions(store, pl.id, now)
            entry = next(e for e in restored if e['ytm']==pl.provider_playlist_id and e['identity']==pl.account_id)
            if owned:
                entry['phase'] = 'uncertain'
                _record()
            try:
                added, removed, prior, skipped, expected = _reconcile(
                    client_for(pl), pl.provider_playlist_id, result_video_ids)
            except PlaylistAddUncertain as exc:
                addition = AdditionOutcome(exc.confirmed, exc.skipped, exc)
                summary["added"] += addition.added
                summary["skipped"] += len(addition.skipped)
                entry = next(e for e in restored if e["ytm"] == pl.provider_playlist_id and e['identity']==pl.account_id)
                # This read may contain independent concurrent additions. It is
                # valid mirror data, but cannot establish ownership for Undo.
                entry["post_state"] = None
                entry["addition"] = addition.diagnostics()
                retain_addition_snapshot(store, pl.id, addition, now)
                raise
            summary["added"] += added
            summary["removed"] += removed
            summary["skipped"] += len(skipped)
            entry = next(e for e in restored if e["ytm"] == pl.provider_playlist_id and e['identity']==pl.account_id)
            entry["prev"] = prior
            entry["post_state"] = _capture_post(client_for(pl), pl.provider_playlist_id, expected_videos=expected)
            if entry["post_state"] is None:
                raise ValueError("Couldn't verify the merged playlist contents. Refusing to delete any source. "
                                 "Re-sync and check again.")
            desired = [v for v in result_video_ids if v not in skipped]
            if expected != desired:
                # Use the current occurrence handles from the verified membership read.
                # Once positional writes start, that baseline is no longer Undo evidence.
                state, entry["post_state"] = entry["post_state"], None
                tracks = [{"videoId": video, "setVideoId": handle}
                          for video, handle in zip(state["videos"], state["handles"])]
                indices = {track["videoId"]: index for index, track in enumerate(tracks)}
                apply_playlist_permutation(client_for(pl), pl.provider_playlist_id, tracks,
                                           [indices[video] for video in desired])
                entry["post_state"] = _capture_post(client_for(pl), pl.provider_playlist_id,
                                                    expected_videos=desired)
                if entry["post_state"] is None:
                    raise ValueError("Couldn't verify the merged playlist order. Refusing to delete any source. "
                                     "Re-sync and check again.")
            if owned:
                entry['phase'] = 'complete'
                _record()
        if droppers:
            _record()
            deletion_locks.enter_context(claim_action(store, action_id, operation="Deletion"))
        for pl in droppers:
            # Exclusions only apply to uploads the editor could show, never unseen remote changes.
            excluded = {row[1] for row in _tracks_with_meta(store, pl.id) if row[1]} - retained
            _verify_and_delete(
                store, pl.id, client_for(pl), client_for(keepers[0]), keepers[0].provider_playlist_id, now,
                retained_video_ids=retained, excluded_video_ids=excluded, backups=backups,
                action_id=action_id, locked=True,target_locked=owned)
            summary["deleted"].append(pl.title)
    except Exception as e:
        extra = {"partial": True, "error": str(e)}
        if isinstance(e, _MissingRequiredTracks):
            extra["missing_required"] = e.count
        _record(extra)  # partial merges must remain undoable
        raise
    else:
        _record()
    finally:
        deletion_locks.close()
    return summary

def copy_or_move_playlist(store, playlist_id, target_account_id, source_client, target_client, now,
                          *, delete_source=False, fuzzy_threshold=0.85) -> dict:
    """Recreate a playlist under another identity (copy); optionally delete the original (move).

    Tracks are resolved in the target identity's context (reuse the videoId, else fuzzy-search).
    A move only deletes the source after fresh remote reads verify every required upload.
    An incomplete or uncertain transfer stays a copy, with the source kept and an undoable action.
    """
    src = store.playlists.get_playlist(playlist_id)
    if src is None:
        raise ValueError("playlist no longer exists")
    if src.account_id == target_account_id:
        raise ValueError("the source and target identity are the same")
    rows = _tracks_with_meta(store, playlist_id)
    new_pid = target_client.create_playlist(src.title, "Copied by TuneConsole")
    video_ids, unresolved, metadata = [], 0, {}
    for key, vid, title, artist, dur, _avail in rows:
        res = _resolve_in_target(target_client, key, title, artist, vid, dur, fuzzy_threshold)
        if res.target_video_id:
            video_ids.append(res.target_video_id)
            metadata[res.target_video_id] = (title, artist, dur)
        else:
            unresolved += 1
    addition = account_additions(_add_items, target_client, new_pid, video_ids, prior_tracks=[])
    added, skipped = addition.added, addition.skipped
    # Persist the destination even when the next operation must report an uncertain transfer.
    tid_by_vid = store.tracks.track_ids_for_videos(addition.confirmed)
    tids = []
    for video in addition.confirmed:
        title, artist, duration = metadata[video]
        tids.append(tid_by_vid.get(video) or store.tracks.upsert_track(video, title, artist, None, duration))
    keys = [store.tracks.catalog_subject(tid)["identity_key"] for tid in tids]
    db_pid = store.playlists.upsert_playlist(target_account_id, new_pid, src.title, len(tids),
                                            content_hash(list(dict.fromkeys(keys))), now, created_at=now)
    store.playlists.set_playlist_tracks(db_pid, tids)
    store.playlists.ensure_playlist_thumbnail(db_pid)
    retain_addition_snapshot(store, db_pid, addition, now)
    deleted, delete_error, missing_required = False, None, None
    backups = []
    undo = capture_copy(target_client, new_pid, target_account_id,
                      [v for v in video_ids if v not in skipped],
                      title=src.title, description="Copied by TuneConsole", addition=addition)
    action_id = None

    def _record_move():
        nonlocal action_id
        params = json.dumps({"src_ytm": src.provider_playlist_id, "title": src.title,
                             "target_identity": target_account_id, "new_ytm": new_pid,
                             "deleted": deleted, "added": added, "skipped": len(skipped),
                             "unresolved": unresolved, "addition": addition.diagnostics(),
                             "partial": bool(addition.failure), "delete_error": delete_error,
                             "missing_required": missing_required,
                             "source_backup": backups[0] if backups else None})
        if action_id is None:
            action_id = store.actions.record_action(
                MOVE_IDENTITY, params, "{}", "executed", json.dumps(undo), now)
        else:
            action = store.actions.get_action(action_id)
            if action.status in ("executed", "delete_pending", "delete_failed"):
                store.actions.update_action(action_id, action.status, now, params_json=params)

    if delete_source and unresolved == 0 and not skipped and not addition.failure:
        _record_move()  # preserve destination recovery before beginning source deletion
        try:
            # Not every playlist is deletable (auto-generated/system playlists 400 here). The copy
            # already succeeded, so a failed delete must not strip the copy. Keep both and report.
            with claim_action(store, action_id, operation="Deletion"):
                _verify_and_delete(store, playlist_id, source_client, target_client, new_pid, now,
                                   retained_video_ids=video_ids, backups=backups,
                                   action_id=action_id, locked=True)
                deleted = True
        except Exception as e:  # noqa: BLE001
            logger.warning("move: keeping source %s: %s", src.provider_playlist_id, e)
            delete_error = str(e) or type(e).__name__
            if isinstance(e, _MissingRequiredTracks):
                missing_required = e.count
    _record_move()
    addition.finish()
    return {"added": added, "skipped": len(skipped), "unresolved": unresolved, "deleted": deleted,
            "title": src.title, "delete_error": delete_error}

def forget_empty_playlist(store, playlist_id, now, *, reason="user_requested") -> None:
    """Drop only an empty local membership, leaving YouTube and durable curation untouched."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ActionInputError('playlist_missing')
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ActionInputError('playlist_system_removal')
    store.playlists.remove_playlist(playlist_id, require_empty=True)
    store.actions.record_action(
        REMOVE_LOCAL_PLAYLIST,
        json.dumps({"ytm": pl.provider_playlist_id, "title": pl.title, "reason": reason}),
        "{}", "executed", "{}", now)


def delete_empty_playlist(store, playlist_id, client, now) -> str | None:
    """Claim the target, then confirm emptiness before backing up and deleting it."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ActionInputError('playlist_missing')
    with claim_playlist_deletion(store, pl):
        return _delete_empty_playlist(store, playlist_id, client, now)


def _delete_empty_playlist(store, playlist_id, client, now):
    """Delete only after a fresh remote read confirms emptiness; back up and prune the row.

    Some empty playlists fail to parse, but that failure cannot prove emptiness: the same failure
    can hide newly added tracks. Cached track counts never authorize deletion after a failed read.
    An explicit remote 404 only removes an empty local entry, without a remote write or backup.
    """
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ActionInputError('playlist_missing')
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ActionInputError('playlist_system_removal')
    resumed = resume_pending_deletion(store, pl, client, now)
    if resumed is not None:
        return resumed
    try:
        snapshot = with_retry(lambda: playlist_if_present(client, pl.provider_playlist_id))
    except Exception as exc:
        raise ValueError(f"Couldn't confirm “{pl.title}” is empty on YouTube. "
                         "Re-sync and try again. Nothing was changed.") from exc
    if snapshot is None:
        forget_empty_playlist(store, playlist_id, now, reason="remote_missing")
        return None
    if snapshot["tracks"]:
        raise ActionInputError('playlist_not_empty')
    backup_path = backup_playlist(store, playlist_id, now, client, snapshot=snapshot)
    logger.warning("deleting empty playlist %s (backup at %s)", pl.provider_playlist_id, backup_path)
    delete_backed_up(store, pl, client, backup_path, now, kind=DELETE_EMPTY)
    return backup_path

def delete_playlist(store, playlist_id, client, now) -> str:
    """Claim and back up one playlist before deleting it with durable recovery."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    with claim_playlist_deletion(store, pl):
        return _delete_playlist(store, playlist_id, client, now)


def _delete_playlist(store, playlist_id, client, now):
    """Delete a playlist outright (any size). Backs up first, prunes the row, records an undoable action.

    Unlike delete_empty_playlist, this does not require the playlist to be empty. It's the
    Playlists-tab bulk delete. System playlists are refused by backup_playlist.
    """
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    resumed = resume_pending_deletion(store, pl, client, now)
    if resumed is not None:
        return resumed
    backup_path = backup_playlist(store, playlist_id, now, client)
    logger.warning("deleting playlist %s (backup at %s)", pl.provider_playlist_id, backup_path)
    delete_backed_up(store, pl, client, backup_path, now, kind=DELETE_PLAYLIST)
    return backup_path


def add_tracks_to_playlist(store, playlist_id, tracks, client, now, after_video_id=None, *,
                           prior_tracks=None) -> dict:
    """Append the given tracks (full metadata dicts) to an existing playlist on YouTube, then seed
    the store directly from what we added (no racing read-back). When `after_video_id` is an existing
    track, the freshly-added tracks are then slotted just below it (preserving their order) rather than
    left at the end. Returns counts."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ActionInputError('playlist_missing')
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ActionInputError('playlist_regular_required')
    items = [t for t in tracks if t.get("videoId")]
    if not items:
        raise ActionInputError('playlist_tracks_required')
    if after_video_id and after_video_id not in {t["video_id"] for t in store.charts.playlist_tracks_detail(playlist_id)}:
        raise ActionInputError('playlist_song_missing')
    handles = {}
    addition = account_additions(add_items_resilient, client, pl.provider_playlist_id,
                                 [t["videoId"] for t in items], set_video_ids=handles,
                                 prior_tracks=prior_tracks)
    added, skipped = addition.added, addition.skipped
    confirmed = set(addition.confirmed)
    existing = store.playlists.get_playlist_track_ids(playlist_id)
    new_ids, titles = [], []
    for t in items:
        if t["videoId"] not in confirmed:
            continue
        # The track time may be missing (search often omits it). Fetch it now, but only for the
        # handful of tracks actually being added, so the playlist row shows a real duration (#26).
        dur = t.get("duration")
        if dur is None:
            dur = fetch_song_duration(client, t["videoId"])
        new_ids.append(store.tracks.upsert_track(t["videoId"], t.get("title", ""), t.get("artist"),
                                          t.get("album"), dur, 1,
                                          None, None, t.get("album_browse"), t.get("thumbnail")))
        titles.append(t.get("title", ""))
    # Show the requested order immediately, but retain a durable pending move until YouTube confirms.
    new_unique = list(dict.fromkeys(i for i in new_ids if i not in existing))
    insert_at = len(existing)
    if after_video_id:
        anchor_tid = store.tracks.track_ids_for_videos([after_video_id]).get(after_video_id)
        if anchor_tid in existing:
            insert_at = existing.index(anchor_tid) + 1
    combined = existing[:insert_at] + new_unique + existing[insert_at:]
    store.playlists.set_playlist_tracks(playlist_id, combined)
    store.playlists.set_playlist_track_count(playlist_id, len(combined), now)
    store.playlists.ensure_playlist_thumbnail(playlist_id)
    retain_addition_snapshot(store, playlist_id, addition, now)
    position_plan = {}
    if after_video_id and new_unique and not addition.failure:
        tmap = store.tracks.track_ids_for_videos([t["videoId"] for t in items])
        video_ids = [t["videoId"] for t in items if t["videoId"] in confirmed
                     and tmap.get(t["videoId"]) in new_unique]
        position_plan = {"playlist_id": playlist_id, "after_video_id": after_video_id,
                         "video_ids": list(dict.fromkeys(video_ids)), "set_video_ids": handles}
    store.actions.record_action(ADD_TRACKS,
                        json.dumps({"playlist": pl.title, "added": added, "titles": titles,
                                    "partial": bool(addition.failure), "skipped": len(skipped),
                                    "addition": addition.diagnostics()}),
                        json.dumps(position_plan), "pending" if position_plan else "executed",
                        json.dumps({"addition": addition.diagnostics(),
                            "snapshot": addition.failure.snapshot if addition.failure else None}), now)
    addition.finish()
    pending = retry_insertions(store, playlist_id, client, now) if position_plan else 0
    return {"added": added, "skipped": len(skipped), "count": len(combined), "position_pending": pending}


def rename_playlist(store, playlist_id, title, client, now) -> dict:
    """Rename a playlist on YouTube and in the store."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ActionInputError('playlist_missing')
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ActionInputError('playlist_regular_required')
    title = (title or "").strip()
    if not title:
        raise ActionInputError('playlist_name_required')
    confirm_playlist_write(
        lambda: client.edit_playlist(pl.provider_playlist_id, title=title),
        lambda: _remote_snapshot(client, pl.provider_playlist_id),
        lambda detail: detail.get("title") == title, "rename")
    store.playlists.set_playlist_title(playlist_id, title, now)
    store.actions.record_action(RENAME_PLAYLIST,
                        json.dumps({"from": pl.title, "to": title}), "{}", "executed", "{}", now)
    return {"title": title}


def remove_track(store, playlist_id, video_id, client, now) -> dict:
    """Remove a single track from a real playlist (YouTube + store). Liked Music is handled upstream
    by LikedMusic.remove (an unlike), so by here a system playlist is always a hard error."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ActionInputError('playlist_missing')
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ActionInputError('playlist_regular_required')
    tracks = _remote_snapshot(client, pl.provider_playlist_id)["tracks"]
    index = next((i for i, track in enumerate(tracks) if track["videoId"] == video_id), None)
    if index is None or not tracks[index].get("setVideoId"):
        raise ValueError("track not found on YouTube (already removed?)")
    expected = tracks[:index] + tracks[index + 1:]
    confirm_playlist_write(
        lambda: client.remove_playlist_items(pl.provider_playlist_id, [tracks[index]]),
        lambda: _remote_snapshot(client, pl.provider_playlist_id)["tracks"],
        lambda actual: same_playlist_items(actual, expected), "track removal")
    cancel_insertions(store, playlist_id, now)
    moved_tid = store.tracks.track_ids_for_videos([video_id]).get(video_id)
    # A second occurrence of the same recording still belongs in the local membership.
    retained = any(track["videoId"] == video_id for track in expected)
    ids = [i for i in store.playlists.get_playlist_track_ids(playlist_id) if retained or i != moved_tid]
    store.playlists.set_playlist_tracks(playlist_id, ids)
    store.playlists.set_playlist_track_count(playlist_id, len(ids), now)
    store.actions.record_action(REMOVE_TRACK,
                        json.dumps({"playlist": pl.title, "video_id": video_id}),
                        "{}", "executed", "{}", now)
    return {"count": len(ids)}


def reorder_track(store, playlist_id, video_id, before_video_id, client, now) -> dict:
    """Move `video_id` so it sits just before `before_video_id` (or to the end if that's empty),
    on YouTube and in the store. One move per call. Matches a single drag-and-drop."""
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("can't reorder a system playlist")
    if not video_id or video_id == before_video_id:
        return {"ok": True}
    tracks = _remote_snapshot(client, pl.provider_playlist_id)["tracks"]
    moved = next((i for i, track in enumerate(tracks) if track["videoId"] == video_id), None)
    if moved is None:
        raise ValueError("track not found on YouTube")
    successor = next((i for i, track in enumerate(tracks) if track["videoId"] == before_video_id), None)
    if before_video_id and successor is None:
        raise ValueError("the requested destination track is not on YouTube. Re-sync before reordering")
    target = [i for i in range(len(tracks)) if i != moved]
    target.insert(target.index(successor) if successor is not None else len(target), moved)
    apply_playlist_permutation(client, pl.provider_playlist_id, tracks, target, movable={moved})
    cancel_insertions(store, playlist_id, now)
    tmap = store.tracks.track_ids_for_videos([video_id] + ([before_video_id] if before_video_id else []))
    moved_tid = tmap.get(video_id)
    ids = [i for i in store.playlists.get_playlist_track_ids(playlist_id) if i != moved_tid]
    before_tid = tmap.get(before_video_id) if before_video_id else None
    if before_tid in ids:
        ids.insert(ids.index(before_tid), moved_tid)
    elif moved_tid is not None:
        ids.append(moved_tid)
    store.playlists.set_playlist_tracks(playlist_id, ids)
    return {"ok": True}


def execute_planned(store, action_id, clients, now) -> None:
    """Execute a stored delete plan: remote-verify the keeper still holds every track of the copy
    being deleted, back it up, delete it, and prune the row. Recorded as an undoable 'plan' action."""
    action = store.actions.get_action(action_id)
    if action is None:
        raise ValueError(f"no such action {action_id}")
    if action.status == "delete_pending":
        from yt_playlist.library.playlist_deletion import reconcile_deletion
        result = reconcile_deletion(store, action_id, clients, now)
        if result.status != "executed":
            raise ValueError("The playlist still exists. Review the saved deletion in the Action Log.")
        return
    if action.status != "planned":
        raise ValueError(f"action {action_id} is {action.status}, not planned")
    pe = deserialize_plan(action)
    if pe.mode != "delete":
        raise ValueError(f"unknown stored mode {pe.mode!r}")
    plan = pe.plan
    src_pl = store.playlists.get_playlist(plan.source_playlist_id)
    tgt_pl = store.playlists.get_playlist(plan.target_playlist_id)
    if src_pl is None or tgt_pl is None:
        raise ValueError("source/target playlist no longer exists")
    _require_distinct_playlists(src_pl, tgt_pl)
    _verify_and_delete(
        store, plan.source_playlist_id, clients[src_pl.account_id], clients[tgt_pl.account_id],
        tgt_pl.provider_playlist_id, now, action_id=action_id)
