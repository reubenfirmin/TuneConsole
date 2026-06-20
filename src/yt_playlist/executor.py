import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from yt_playlist.matching import fuzzy_ratio, normalize, track_artist, identity_key
from yt_playlist.retry import with_retry
from yt_playlist import paths
from yt_playlist.action_kinds import (
    PLAN, APPLY_MERGE, MOVE_IDENTITY, DELETE_EMPTY, UNDO, UNDOABLE_KINDS, is_undoable)
from yt_playlist.analysis import SYSTEM_PLAYLIST_IDS

logger = logging.getLogger(__name__)

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
    source_ytm_playlist_id: str

def serialize_plan(plan, mode, source_ytm_playlist_id, source_title=None, target_title=None):
    params = {"source": plan.source_playlist_id, "target": plan.target_playlist_id,
              "mode": mode, "source_ytm": source_ytm_playlist_id,
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

def store_plan(store, plan, mode, source_ytm_playlist_id, now) -> int:
    # capture titles now so the action log can name the playlists even after one is pruned
    src = store.get_playlist(plan.source_playlist_id)
    tgt = store.get_playlist(plan.target_playlist_id)
    params_json, plan_json = serialize_plan(
        plan, mode, source_ytm_playlist_id,
        src.title if src else None, tgt.title if tgt else None)
    return store.record_action(PLAN, params_json, plan_json, "planned", "{}", now)

def _tracks_with_meta(store, playlist_id):
    return store.get_playlist_tracks_with_meta(playlist_id)

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
    if best is not None:
        return Resolution(key, source_vid, best[2], "search")
    return Resolution(key, source_vid, None, "unresolved")

def _target_ytm_id(store, playlist_id):
    return store.get_playlist(playlist_id).ytm_playlist_id

def _remote_keys(client, ytm_playlist_id):
    """Current remote identity_keys of a playlist, or None if it can't be read (deleted/unparseable)."""
    try:
        detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
    except Exception:  # noqa: BLE001 - deleted playlist or ytmusicapi parse failure
        return None
    return {identity_key(t.get("title", ""), track_artist(t)) for t in detail.get("tracks", [])}

def _verify_and_delete(store, plan, source_client, target_client, source_ytm_playlist_id, now) -> str:
    # Safety check against LIVE remote state for BOTH playlists (targeted fetches, not a full sync):
    # we may delete the source only if every track CURRENTLY in it still exists in the kept target.
    # This blocks the "go back and delete the other/surviving copy" foot-gun — the kept playlist is
    # re-read each time, so once a copy is gone the check fails.
    target_ytm = _target_ytm_id(store, plan.target_playlist_id)
    source_keys = _remote_keys(source_client, source_ytm_playlist_id)
    if source_keys is None:  # can't read the source remotely; fall back to its last-synced contents
        source_keys = store.get_playlist_track_keys(plan.source_playlist_id)
    if source_keys:  # empty source loses nothing -> safe to delete without reading the target
        target_keys = _remote_keys(target_client, target_ytm)
        if target_keys is None:
            raise ValueError("couldn't read the kept playlist from YouTube to verify (it may have "
                             "been deleted) — refusing to delete. Nothing was changed.")
        if not source_keys <= target_keys:
            raise ValueError("the kept playlist no longer contains every track of the one you're "
                             "deleting — refusing to delete. Re-sync and check again.")
    backup_path = backup_playlist(store, plan.source_playlist_id, now)
    logger.warning("deleting source playlist %s (backup at %s)", source_ytm_playlist_id, backup_path)
    # Not wrapped in with_retry: delete_playlist is non-idempotent. The subset check above has
    # already confirmed the merge succeeded remotely, so the delete should not be auto-replayed.
    source_client.delete_playlist(source_ytm_playlist_id)
    return backup_path

def _reconcile(client, ytm_playlist_id, desired_video_ids):
    """Make a playlist's contents equal desired_video_ids: add what's missing, remove the extras.

    Returns (n_added, n_removed, prior_video_ids) — prior_video_ids is the contents before the
    change, so an undo can restore it.
    """
    detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
    tracks = detail.get("tracks", [])
    prior = [t.get("videoId") for t in tracks if t.get("videoId")]
    desired = list(dict.fromkeys(v for v in desired_video_ids if v))   # de-dupe, keep order
    desired_set = set(desired)
    current = set(prior)
    to_add = [v for v in desired if v not in current]
    to_remove = [t for t in tracks if t.get("videoId") and t.get("videoId") not in desired_set]
    if to_add:
        client.add_playlist_items(ytm_playlist_id, to_add)
    if to_remove:
        client.remove_playlist_items(ytm_playlist_id, to_remove)
    return len(to_add), len(to_remove), prior

def apply_result(store, clients, playlist_ids, result_video_ids, keep, now) -> dict:
    """N-way track-level merge: set the kept playlist(s) to exactly result_video_ids.

    keep == "all": set every playlist in playlist_ids to the result (keep them all).
    keep == <playlist id>: set that one to the result and delete the others (each backed up first).
    """
    pls = [store.get_playlist(pid) for pid in playlist_ids]
    if any(p is None for p in pls):
        raise ValueError("a playlist no longer exists")
    if len(pls) < 2:
        raise ValueError("need at least two playlists")

    def client_for(pl):
        c = clients.get(pl.identity_id)
        if c is None:
            raise ValueError("no client for that identity")
        return c

    keep_all = (str(keep) == "all")
    keepers = pls if keep_all else [p for p in pls if p.id == int(keep)]
    if not keepers:
        raise ValueError("keep must be 'all' or one of the playlists")
    droppers = [] if keep_all else [p for p in pls if p.id != int(keep)]

    summary = {"added": 0, "removed": 0, "deleted": [],
               "kept_ytm": keepers[0].ytm_playlist_id, "kept_title": keepers[0].title}
    restored, backups = [], []
    for pl in keepers:
        added, removed, prior = _reconcile(client_for(pl), pl.ytm_playlist_id, result_video_ids)
        summary["added"] += added
        summary["removed"] += removed
        restored.append({"ytm": pl.ytm_playlist_id, "identity": pl.identity_id, "prev": prior})
    for pl in droppers:
        backups.append(backup_playlist(store, pl.id, now))
        logger.warning("apply: deleting %s", pl.ytm_playlist_id)
        client_for(pl).delete_playlist(pl.ytm_playlist_id)
        store.remove_playlist(pl.id)
        summary["deleted"].append(pl.title)
    store.record_action(APPLY_MERGE,
                        json.dumps({"deleted": summary["deleted"]}),
                        "{}", "executed", json.dumps({"restored": restored, "backups": backups}), now)
    return summary

def copy_or_move_playlist(store, playlist_id, target_identity_id, source_client, target_client, now,
                          *, delete_source=False, fuzzy_threshold=0.85) -> dict:
    """Recreate a playlist under another identity (copy); optionally delete the original (move).

    Tracks are resolved in the target identity's context (reuse the videoId, else fuzzy-search).
    A move only deletes the source if every track was recreated (else it stays a copy, source kept).
    """
    src = store.get_playlist(playlist_id)
    if src is None:
        raise ValueError("playlist no longer exists")
    if src.identity_id == target_identity_id:
        raise ValueError("the source and target identity are the same")
    rows = _tracks_with_meta(store, playlist_id)
    new_pid = target_client.create_playlist(src.title, "Copied by yt-playlist")
    video_ids, unresolved = [], 0
    for key, vid, title, artist, dur, _avail in rows:
        res = _resolve_in_target(target_client, key, title, artist, vid, dur, fuzzy_threshold)
        if res.target_video_id:
            video_ids.append(res.target_video_id)
        else:
            unresolved += 1
    if video_ids:
        target_client.add_playlist_items(new_pid, video_ids)
    deleted, backup_path, delete_error = False, None, None
    if delete_source and unresolved == 0:
        bpath = backup_playlist(store, playlist_id, now)
        try:
            # Not every playlist is deletable (auto-generated/system playlists 400 here). The copy
            # already succeeded, so a failed delete must not strip the copy — keep both and report.
            source_client.delete_playlist(src.ytm_playlist_id)
            store.remove_playlist(playlist_id)
            deleted, backup_path = True, bpath
        except Exception as e:  # noqa: BLE001
            logger.warning("move: copied %s but could not delete source: %s", src.ytm_playlist_id, e)
            delete_error = str(e) or type(e).__name__
    store.record_action(
        MOVE_IDENTITY,
        json.dumps({"src_ytm": src.ytm_playlist_id, "title": src.title,
                    "target_identity": target_identity_id, "new_ytm": new_pid, "deleted": deleted}),
        "{}", "executed",
        json.dumps({"backup": backup_path, "new_ytm": new_pid, "target_identity": target_identity_id}), now)
    return {"added": len(video_ids), "unresolved": unresolved, "deleted": deleted,
            "title": src.title, "delete_error": delete_error}

def delete_empty_playlist(store, playlist_id, client, now) -> str:
    """Delete a playlist only if it's (still) empty remotely. Backs up, deletes, prunes the row.

    Empty playlists often can't be parsed by ytmusicapi (get_playlist raises) — _remote_keys returns
    None in that case, which we treat as empty. If it actually has tracks now, we refuse.
    """
    pl = store.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    keys = _remote_keys(client, pl.ytm_playlist_id)
    if keys:
        raise ValueError(f"“{pl.title}” isn't empty anymore ({len(keys)} tracks) — re-sync first")
    backup_path = backup_playlist(store, playlist_id, now)
    logger.warning("deleting empty playlist %s (backup at %s)", pl.ytm_playlist_id, backup_path)
    client.delete_playlist(pl.ytm_playlist_id)
    store.remove_playlist(playlist_id)
    store.record_action(DELETE_EMPTY,
                        json.dumps({"ytm": pl.ytm_playlist_id, "title": pl.title}),
                        "{}", "executed", json.dumps({"backup": backup_path}), now)
    return backup_path

def backup_playlist(store, playlist_id, now) -> str:
    pl = store.get_playlist(playlist_id)
    ytm = pl.ytm_playlist_id
    # central guard: every delete path backs up first, so refusing here keeps undeletable
    # system playlists (Liked Music, Episodes for Later) from ever being targeted.
    if ytm in SYSTEM_PLAYLIST_IDS:
        raise ValueError(f"“{pl.title}” is a system playlist and can't be deleted")
    tracks = [{"identity_key": k, "video_id": v, "title": t, "artist": a, "duration_s": d}
              for (k, v, t, a, d, _avail) in _tracks_with_meta(store, playlist_id)]
    payload = {"playlist_id": playlist_id, "ytm_playlist_id": ytm, "identity_id": pl.identity_id,
               "title": pl.title, "tracks": tracks}
    # ytm comes from the YouTube API (untrusted); strip anything that could escape
    # the backups dir (e.g. "/" or "..") before using it in a filename.
    safe_ytm = re.sub(r"[^A-Za-z0-9_-]", "_", ytm)
    filename = f"{safe_ytm}_{playlist_id}_{int(now)}.json"
    backups = paths.backups_dir().resolve()
    path = (backups / filename).resolve()
    if path.parent != backups:  # belt-and-suspenders: must stay inside backups dir
        raise ValueError(f"refusing to write backup outside {backups}")
    path.write_text(json.dumps(payload, indent=2))
    return str(path)

def execute_planned(store, action_id, clients, now) -> None:
    """Execute a stored delete plan: remote-verify the keeper still holds every track of the copy
    being deleted, back it up, delete it, and prune the row. Recorded as an undoable 'plan' action."""
    action = store.get_action(action_id)
    if action is None:
        raise ValueError(f"no such action {action_id}")
    if action.status != "planned":
        raise ValueError(f"action {action_id} is {action.status}, not planned")
    pe = deserialize_plan(action)
    if pe.mode != "delete":
        raise ValueError(f"unknown stored mode {pe.mode!r}")
    plan = pe.plan
    src_pl = store.get_playlist(plan.source_playlist_id)
    tgt_pl = store.get_playlist(plan.target_playlist_id)
    if src_pl is None or tgt_pl is None:
        raise ValueError("source/target playlist no longer exists")
    if plan.source_playlist_id == plan.target_playlist_id:
        raise ValueError("source and target must differ")
    backup_path = _verify_and_delete(
        store, plan, clients[src_pl.identity_id], clients[tgt_pl.identity_id], pe.source_ytm_playlist_id, now)
    store.remove_playlist(plan.source_playlist_id)  # drop it from the dashboard immediately
    store.update_action(action_id, "executed", now, undo_json=json.dumps({"backup": backup_path}))

def _client_for(clients, identity_id):
    if identity_id not in clients:
        raise ValueError(f"no client for identity {identity_id}")
    return clients[identity_id]

def _recreate_from_backup(clients, backup_path):
    """Recreate a deleted playlist from its JSON backup, under the identity it belonged to."""
    payload = json.loads(Path(backup_path).read_text())
    identity_id = payload.get("identity_id")
    if identity_id is None:
        raise ValueError("backup has no identity; cannot recreate")
    client = _client_for(clients, identity_id)
    new_pid = client.create_playlist(payload["title"], "Recreated by yt-playlist undo")
    vids = [t["video_id"] for t in payload.get("tracks", []) if t.get("video_id")]
    if vids:
        client.add_playlist_items(new_pid, vids)
    logger.warning("undo: recreated %r as %s", payload.get("title"), new_pid)
    return new_pid

def undo_action(store, action_id, clients, now) -> None:
    action = store.get_action(action_id)
    if action is None:
        raise ValueError(f"no such action {action_id}")
    if action.status != "executed" or not is_undoable(action.kind):
        raise ValueError(f"action {action_id} ({action.kind}/{action.status}) is not undoable")
    undo = json.loads(action.undo_json or "{}")

    if action.kind != PLAN:
        # apply_merge / move_identity / delete_empty: restore prior contents, recreate deletions
        for entry in undo.get("restored", []):          # apply_merge: kept playlists back to prior
            client = _client_for(clients, entry["identity"])
            _reconcile(client, entry["ytm"], entry.get("prev", []))
        if action.kind == MOVE_IDENTITY and undo.get("new_ytm") is not None:
            try:                                          # remove the copy created in the target
                _client_for(clients, undo["target_identity"]).delete_playlist(undo["new_ytm"])
            except Exception:  # noqa: BLE001
                logger.warning("undo: could not delete recreated copy %s", undo.get("new_ytm"))
        backups = list(undo.get("backups", []))
        if undo.get("backup"):
            backups.append(undo["backup"])
        for bp in backups:
            _recreate_from_backup(clients, bp)
        store.record_action(UNDO, json.dumps({"undid": action_id}), "{}", "executed", "{}", now)
        store.update_action(action_id, "undone", now)
        return

    pe = deserialize_plan(action)
    added_vids = {r.target_video_id for r in pe.plan.additions if r.target_video_id}
    tgt_pl = store.get_playlist(pe.plan.target_playlist_id)
    if tgt_pl is not None and added_vids:
        tclient = _client_for(clients, tgt_pl.identity_id)
        target_ytm = tgt_pl.ytm_playlist_id
        detail = with_retry(lambda: tclient.get_playlist(target_ytm, limit=None))
        items = [t for t in detail.get("tracks", []) if t.get("videoId") in added_vids]
        if items:
            # Non-idempotent mutation: not retried (a replay could remove the wrong items if the
            # playlist changed between a lost response and the retry).
            tclient.remove_playlist_items(target_ytm, items)
    backup = json.loads(action.undo_json or "{}").get("backup")
    if backup:  # the action deleted a source playlist -> recreate it from the backup
        payload = json.loads(Path(backup).read_text())
        # the row was pruned from the store on deletion, so fall back to the backup's identity_id
        src_pl = store.get_playlist(pe.plan.source_playlist_id)
        identity_id = src_pl.identity_id if src_pl is not None else payload.get("identity_id")
        if identity_id is None:
            raise ValueError(f"source playlist {pe.plan.source_playlist_id} no longer in store and "
                             "backup has no identity; cannot recreate")
        sclient = _client_for(clients, identity_id)
        # create_playlist requires a description (real YTMusic API); non-idempotent, so not retried.
        new_pid = sclient.create_playlist(payload["title"], "Recreated by yt-playlist undo")
        vids = [t["video_id"] for t in payload.get("tracks", []) if t.get("video_id")]
        if vids:
            sclient.add_playlist_items(new_pid, vids)
        logger.warning("undo: recreated source playlist %r as %s", payload.get("title"), new_pid)
    store.record_action(UNDO, json.dumps({"undid": action_id}), "{}", "executed", "{}", now)
    store.update_action(action_id, "undone", now)
