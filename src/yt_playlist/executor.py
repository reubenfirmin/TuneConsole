import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from yt_playlist.matching import fuzzy_ratio, normalize, track_artist, identity_key
from yt_playlist.retry import with_retry
from yt_playlist import paths
from yt_playlist.action_kinds import (
    PLAN, APPLY_MERGE, MOVE_IDENTITY, DELETE_EMPTY, DELETE_PLAYLIST, GC_GENERATED, COPY_PLAYLIST,
    COPY_INTO, ADD_TRACKS, REMOVE_TRACK, RENAME_PLAYLIST, UNDO, UNDOABLE_KINDS, is_undoable)
from yt_playlist.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.thumbnails import best_thumb

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
    # Only accept a search match we're confident is the SAME recording: either its duration matches
    # (within 3s) or the title/artist is near-exact. A merely-similar title (e.g. a 7-min extended mix
    # for the 3-min original) is left unresolved, so a move won't delete the source for a wrong substitute.
    if best is not None and (best[0] or best[1] >= 0.95):
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

def _remote_video_ids(client, ytm_playlist_id):
    """Current remote videoIds (in order) of a playlist — for capturing prior contents before a merge."""
    detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
    return [t.get("videoId") for t in detail.get("tracks", []) if t.get("videoId")]

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

def _add_items(client, ytm_playlist_id, video_ids):
    """Add items, surviving a batch rejection. YouTube 400s the WHOLE add_playlist_items call if any
    single videoId is invalid/unavailable, which would otherwise kill an entire merge. So on failure
    we retry one id at a time and skip the bad ones. Returns (added_count, skipped_video_ids)."""
    if not video_ids:
        return 0, []
    try:
        client.add_playlist_items(ytm_playlist_id, video_ids)
        return len(video_ids), []
    except Exception:  # noqa: BLE001 - usually one unavailable id poisoning the batch
        logger.warning("batch add of %d items to %s failed; retrying individually",
                       len(video_ids), ytm_playlist_id)
        added, skipped = 0, []
        for v in video_ids:
            try:
                client.add_playlist_items(ytm_playlist_id, [v])
                added += 1
            except Exception:  # noqa: BLE001
                skipped.append(v)
        if skipped:
            logger.warning("skipped %d unaddable item(s) for %s", len(skipped), ytm_playlist_id)
        return added, skipped

def _reconcile(client, ytm_playlist_id, desired_video_ids):
    """Make a playlist's contents equal desired_video_ids: add what's missing, remove the extras.

    Returns (n_added, n_removed, prior_video_ids, skipped_video_ids) — prior is the contents before
    the change (so undo can restore it); skipped are ids YouTube refused to add.
    """
    detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
    tracks = detail.get("tracks", [])
    prior = [t.get("videoId") for t in tracks if t.get("videoId")]
    desired = list(dict.fromkeys(v for v in desired_video_ids if v))   # de-dupe, keep order
    desired_set = set(desired)
    current = set(prior)
    to_add = [v for v in desired if v not in current]
    to_remove = [t for t in tracks if t.get("videoId") and t.get("videoId") not in desired_set]
    added, skipped = _add_items(client, ytm_playlist_id, to_add)
    if to_remove:
        client.remove_playlist_items(ytm_playlist_id, to_remove)
    return added, len(to_remove), prior, skipped

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
    # YouTube auto-manages system playlists (Liked Music, Episodes for Later) and rejects every
    # added track. Merging *into* one would silently add nothing yet still delete the source — refuse.
    sysk = next((p for p in keepers if p.ytm_playlist_id in SYSTEM_PLAYLIST_IDS), None)
    if sysk is not None:
        raise ValueError(f"can't merge into “{sysk.title}” — YouTube manages that playlist and won't "
                         "accept added tracks. Pick a different playlist to keep.")
    droppers = [] if keep_all else [p for p in pls if p.id != int(keep)]

    summary = {"added": 0, "removed": 0, "skipped": 0, "deleted": [],
               "kept_ytm": keepers[0].ytm_playlist_id, "kept_title": keepers[0].title}
    # Capture every keeper's prior contents BEFORE mutating, so a partial failure (a failed remove, or
    # a dropper delete that throws mid-loop) still leaves a complete, undoable trail — without this the
    # function exited before record_action and the user had a half-merged playlist with no undo.
    restored = [{"ytm": pl.ytm_playlist_id, "identity": pl.identity_id,
                 "prev": _remote_video_ids(client_for(pl), pl.ytm_playlist_id)} for pl in keepers]
    backups = []

    def _record(extra=None):
        store.record_action(APPLY_MERGE,
                            json.dumps({"kept": summary["kept_title"], "deleted": summary["deleted"],
                                        "members": [p.title for p in pls], **(extra or {})}),
                            "{}", "executed", json.dumps({"restored": restored, "backups": backups}), now)

    try:
        for pl in keepers:
            added, removed, _prior, skipped = _reconcile(client_for(pl), pl.ytm_playlist_id, result_video_ids)
            summary["added"] += added
            summary["removed"] += removed
            summary["skipped"] += len(skipped)
        for pl in droppers:
            backups.append(backup_playlist(store, pl.id, now))
            logger.warning("apply: deleting %s", pl.ytm_playlist_id)
            client_for(pl).delete_playlist(pl.ytm_playlist_id)
            store.remove_playlist(pl.id)
            summary["deleted"].append(pl.title)
    except Exception:
        _record({"partial": True})   # record what we managed, so the partial merge is undoable
        raise
    _record()
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
    new_pid = target_client.create_playlist(src.title, "Copied by TuneConsole")
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
    # keys is None only when the remote read failed. That's the EXPECTED look of a genuinely-empty
    # playlist (ytmusicapi can't parse an empty one) — but it's also what a transient network error
    # looks like. Only trust "None == empty" when our last sync also saw it empty; if the store still
    # shows tracks, refuse rather than delete a non-empty playlist on a momentary read failure.
    if keys is None and pl.track_count:
        raise ValueError(f"couldn't confirm “{pl.title}” is empty (it last had {pl.track_count} "
                         "tracks) — re-sync and try again. Nothing was changed.")
    backup_path = backup_playlist(store, playlist_id, now)
    logger.warning("deleting empty playlist %s (backup at %s)", pl.ytm_playlist_id, backup_path)
    client.delete_playlist(pl.ytm_playlist_id)
    store.remove_playlist(playlist_id)
    store.record_action(DELETE_EMPTY,
                        json.dumps({"ytm": pl.ytm_playlist_id, "title": pl.title}),
                        "{}", "executed", json.dumps({"backup": backup_path}), now)
    return backup_path

def delete_playlist(store, playlist_id, client, now) -> str:
    """Delete a playlist outright (any size). Backs up first, prunes the row, records an undoable action.

    Unlike delete_empty_playlist, this does not require the playlist to be empty — it's the
    Playlists-tab bulk delete. System playlists are refused by backup_playlist.
    """
    pl = store.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    backup_path = backup_playlist(store, playlist_id, now)
    logger.warning("deleting playlist %s (backup at %s)", pl.ytm_playlist_id, backup_path)
    client.delete_playlist(pl.ytm_playlist_id)
    store.remove_playlist(playlist_id)
    store.record_action(DELETE_PLAYLIST,
                        json.dumps({"ytm": pl.ytm_playlist_id, "title": pl.title}),
                        "{}", "executed", json.dumps({"backup": backup_path}), now)
    return backup_path

# Keep a generated playlist if at least this fraction of its tracks were played since it was created
# — evidence you actually played the playlist, not just stumbled across a song or two from it.
GC_PLAYED_FRACTION = 0.5


def gc_generated_playlists(store, clients, now, grace_days=None) -> list[dict]:
    """Garbage-collect generated playlists that have gone (mostly) unplayed past their grace window.

    A generated playlist (group == GENERATED_GROUP) is collected when BOTH hold:
      • it's older than the grace window (now - first_seen ≥ grace_days), and
      • fewer than GC_PLAYED_FRACTION of its tracks have been played since it was created.

    YouTube exposes no per-playlist play count, so "played" is judged at the song level using each
    track's last-played date: playing the playlist through lights up most of its tracks within the
    window, whereas a couple of stray plays (a song heard via radio/autoplay) doesn't clear the bar.
    Each collection backs up, deletes locally + on YouTube, and records an undoable GC_GENERATED
    action — exactly like a manual delete, just automatic. Returns the collected playlists.
    """
    from yt_playlist.repos.rec import GENERATED_GROUP
    from yt_playlist import rec_params
    if grace_days is None:
        grace_days = rec_params.get_param(store, "generated_gc_days")
    grace_s = grace_days * 86400.0
    clients = clients or {}
    groups = store.get_playlist_groups()                 # ytm -> group name
    recency = store.get_playlist_track_recency()         # pid -> [per-track last-played ts | None, ...]
    collected = []
    for pl in store.get_playlists():
        if groups.get(pl.ytm_playlist_id) != GENERATED_GROUP:
            continue
        created = pl.first_seen or now
        if now - created < grace_s:                      # still inside its grace window — leave it
            continue
        lasts = recency.get(pl.id) or []
        if lasts:                                        # fraction of tracks played since creation
            played = sum(1 for t in lasts if t is not None and t >= created)
            if played / len(lasts) >= GC_PLAYED_FRACTION:   # enough of it was played -> keep
                continue
        client = clients.get(pl.identity_id)
        if client is None:                                # can't delete remotely without its client
            continue
        try:
            backup_path = backup_playlist(store, pl.id, now)
            logger.warning("GC: deleting unplayed generated playlist %s (backup at %s)",
                           pl.ytm_playlist_id, backup_path)
            client.delete_playlist(pl.ytm_playlist_id)
            store.remove_playlist(pl.id)
            store.record_action(GC_GENERATED,
                                json.dumps({"ytm": pl.ytm_playlist_id, "title": pl.title}),
                                "{}", "executed", json.dumps({"backup": backup_path}), now)
            collected.append({"ytm": pl.ytm_playlist_id, "title": pl.title, "backup": backup_path})
        except Exception:  # noqa: BLE001 - one playlist's failure must not stop the sweep
            logger.warning("GC: could not delete generated playlist %s", pl.ytm_playlist_id,
                           exc_info=True)
    return collected

def copy_playlist(store, playlist_ids, new_name, client, now) -> dict:
    """Copy one or more playlists into a NEW playlist (non-destructive). With several, it's a
    copy+merge: their tracks are unioned (de-duped, order preserved). Pulled into the store."""
    pls = [p for p in (store.get_playlist(i) for i in playlist_ids) if p is not None]
    if not pls:
        raise ValueError("no playlists to copy")
    vids, keys, seen = [], {}, set()
    for pl in pls:
        for (k, v, t, a, d, av) in _tracks_with_meta(store, pl.id):
            if v and v not in seen:
                seen.add(v)
                vids.append(v)
                keys[v] = k
    title = (new_name or "").strip() or (f"{pls[0].title} (copy)" if len(pls) == 1 else "Combined playlist")
    identity = pls[0].identity_id
    new_pid = client.create_playlist(title, "Copied by TuneConsole")
    added, skipped = _add_items(client, new_pid, vids)
    store.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": ", ".join(p.title for p in pls),
                                    "added": added, "skipped": len(skipped)}),
                        "{}", "executed",
                        json.dumps({"new_ytm": new_pid, "target_identity": identity}), now)
    # Seed the store from the tracks we know we added. A read-back from YouTube here would race
    # its indexing lag and often returns an empty playlist; the sources are already in our store,
    # so we map them directly. A later full sync reconciles canonical order/metadata.
    skipped_set = set(skipped)
    added_vids = [v for v in vids if v not in skipped_set]
    tid_by_vid = store.track_ids_for_videos(added_vids)
    track_ids = [tid_by_vid[v] for v in added_vids if v in tid_by_vid]
    from yt_playlist.sync import content_hash   # local import avoids an import cycle
    track_keys = list(dict.fromkeys(keys[v] for v in added_vids if v in keys))
    db_pid = store.upsert_playlist(identity, new_pid, title, len(track_ids), content_hash(track_keys), now)
    store.set_playlist_tracks(db_pid, track_ids)
    return {"new_ytm": new_pid, "title": title, "added": added, "skipped": len(skipped), "from": len(pls)}

def create_generated_playlist(store, title, tracks, client, now, identity_id=None, group=None,
                              recipe=None) -> dict:
    """Create a NEW playlist on YouTube from recommendation tracks (dicts with video_id + metadata),
    tag it `group` ('Generated') so the rec engine quarantines it until it's played, and optimistically
    materialize it into the store so it shows up in the Playlists tab right away (a later sync
    reconciles canonical order/metadata). Non-destructive: nothing existing is touched.

    Quarantine makes the optimistic insert safe even for unowned tracks (e.g. 'fresh songs' not yet in
    your library): the playlist is excluded from playlist-level signals, and its unplayed-only tracks
    are excluded from the embedding baskets — until you actually play it."""
    from yt_playlist.repos.rec import GENERATED_GROUP
    from yt_playlist.sync import content_hash   # local import avoids an import cycle
    group = group or GENERATED_GROUP
    uniq, seen = [], set()
    for t in tracks:
        v = (t or {}).get("video_id")
        if v and v not in seen:
            seen.add(v)
            uniq.append(t)
    if not uniq:
        raise ValueError("no tracks to add")
    title = (title or "Generated playlist").strip()
    if recipe is not None:                                # recipe-driven: version the title + DJ-order
        from yt_playlist.recommend import dj_order, versioned_title
        title = versioned_title(store, title)
        dj = recipe.get("dj", {})
        uniq = dj_order(uniq, stickiness=dj.get("stickiness", 0.0), seed=dj.get("seed", 0))
    new_pid = client.create_playlist(title, "Generated by TuneConsole")
    added, skipped = _add_items(client, new_pid, [t["video_id"] for t in uniq])
    store.set_playlist_group(new_pid, group)             # auto-group (quarantines from the engine)
    db_pid = None
    if identity_id is not None:                          # optimistic local materialization
        skipped_set = set(skipped)
        track_ids, keys = [], []
        for t in uniq:
            if t["video_id"] in skipped_set:
                continue
            track_ids.append(store.upsert_track(
                t["video_id"], t.get("title") or "", t.get("artist") or "", t.get("album") or "",
                None, thumbnail=t.get("thumbnail")))
            keys.append(identity_key(t.get("title") or "", t.get("artist") or ""))
        db_pid = store.upsert_playlist(identity_id, new_pid, title, len(track_ids),
                                       content_hash(list(dict.fromkeys(keys))), now)
        store.set_playlist_tracks(db_pid, track_ids)
    store.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": "recommendations", "group": group,
                                    "added": added, "skipped": len(skipped)}),
                        "{}", "executed", json.dumps({"new_ytm": new_pid}), now)
    if recipe is not None:
        store.set_recipe(new_pid, recipe, now)           # remember exactly how this one was made
    return {"new_ytm": new_pid, "pid": db_pid, "title": title, "added": added, "skipped": len(skipped)}

def create_playlist_from_album(store, browse_id, name, client, now, identity_id) -> dict:
    """Create a NEW playlist from an album's tracks (non-destructive). Fetches the album, creates a
    real (un-grouped) playlist under `identity_id` with its songs, and materializes it into the store
    so it shows up immediately. Returns the new playlist's local db id for redirecting to its page."""
    a = with_retry(lambda: client.get_album(browse_id))
    if not a:
        raise ValueError("couldn't load that album")
    artist = ", ".join(x.get("name", "") for x in (a.get("artists") or []) if x.get("name"))
    vids, seen, meta = [], set(), {}
    for t in (a.get("tracks") or []):
        v = t.get("videoId")
        if v and v not in seen:
            seen.add(v)
            vids.append(v)
            meta[v] = (t.get("title") or "", artist, a.get("title") or "")
    if not vids:
        raise ValueError("this album has no playable tracks")
    title = (name or "").strip() or (a.get("title") or "Album playlist")
    new_pid = client.create_playlist(title, f"Created from “{a.get('title')}” by TuneConsole")
    added, skipped = _add_items(client, new_pid, vids)
    skipped_set = set(skipped)
    thumb = best_thumb(a.get("thumbnails"))
    track_ids, keys = [], []
    for v in vids:
        if v in skipped_set:
            continue
        ti, ar, al = meta[v]
        track_ids.append(store.upsert_track(v, ti, ar, al, None, thumbnail=thumb))
        keys.append(identity_key(ti, ar))
    from yt_playlist.sync import content_hash   # local import avoids an import cycle
    db_pid = store.upsert_playlist(identity_id, new_pid, title, len(track_ids),
                                   content_hash(list(dict.fromkeys(keys))), now)
    store.set_playlist_tracks(db_pid, track_ids)
    store.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": a.get("title") or "album",
                                    "added": added, "skipped": len(skipped)}),
                        "{}", "executed", json.dumps({"new_ytm": new_pid, "target_identity": identity_id}), now)
    return {"new_ytm": new_pid, "db_pid": db_pid, "title": title, "added": added, "skipped": len(skipped)}

def copy_into_playlist(store, source_ids, target_id, client, now) -> dict:
    """Copy the union of tracks from one or more source playlists INTO an existing target playlist
    (non-destructive append). Songs already in the target (by identity_key) are skipped, so it's
    safe to re-run. The source playlists are left untouched."""
    target = store.get_playlist(target_id)
    if target is None:
        raise ValueError("target playlist no longer exists")
    if target.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("can't copy into a system playlist")
    sources = [p for p in (store.get_playlist(i) for i in source_ids) if p is not None and p.id != target_id]
    if not sources:
        raise ValueError("no source playlists to copy")
    # Add with the TARGET's client, so every source video must belong to the target's account. A
    # cross-account copy would feed account A's videoIds to account B's client — region/upload-scoped
    # ids silently 400 and land in `skipped`, leaving the destination short. Require one account.
    if any(p.identity_id != target.identity_id for p in sources):
        raise ValueError("can only copy into a playlist on the same account")
    have = set(store.get_playlist_track_keys(target_id))   # songs already in the target
    vids, seen = [], set()
    for pl in sources:
        for (k, v, t, a, d, av) in _tracks_with_meta(store, pl.id):
            if v and v not in seen and k not in have:
                seen.add(v)
                have.add(k)                                # also de-dupe across sources by song
                vids.append(v)
    added, skipped = _add_items(client, target.ytm_playlist_id, vids)
    skipped_set = set(skipped)
    added_vids = [v for v in vids if v not in skipped_set]
    tid_by_vid = store.track_ids_for_videos(added_vids)
    new_ids = [tid_by_vid[v] for v in added_vids if v in tid_by_vid]
    combined = list(dict.fromkeys(store.get_playlist_track_ids(target_id) + new_ids))
    store.set_playlist_tracks(target_id, combined)
    store.set_playlist_track_count(target_id, len(combined), now)
    store.record_action(COPY_INTO,
                        json.dumps({"target": target.title, "added": added, "skipped": len(skipped),
                                    "source": ", ".join(p.title for p in sources)}),
                        "{}", "executed", "{}", now)
    return {"target_ytm": target.ytm_playlist_id, "title": target.title, "added": added,
            "skipped": len(skipped), "from": len(sources), "count": len(combined)}

def _parse_duration(text):
    """'3:45' / '1:02:03' -> seconds, else None."""
    if not text:
        return None
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


_PARENS_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _strip_parens(text):
    """Drop parenthetical/bracketed qualifiers, e.g. 'Time Zero (Paul Ritch Remix)' -> 'Time Zero'."""
    return _PARENS_RE.sub("", text or "").strip()


def search_versions(client, title, artist, exclude=None, limit=14) -> list:
    """Search YouTube Music for alternate versions of a song. Returns normalized candidates
    (songs first, then videos, then unfiltered), de-duped by videoId, excluding the starting track.

    The query uses the title with remix/live/etc. qualifiers stripped, so a track like
    'Time Zero (Paul Ritch Remix)' searches for 'Time Zero <artist>' and surfaces every version —
    the original and all remixes — rather than only that exact (often-removed) one."""
    base_title = _strip_parens(title) or title
    query = " ".join(x for x in (base_title, artist) if x).strip()
    out, seen = [], set()
    if exclude:
        seen.add(exclude)
    # UNFILTERED first — it mirrors a plain web search (the most relevant top hits, incl. tracks the
    # filtered searches miss) — then songs/videos add structured extras. De-duped by videoId.
    for filt in (None, "songs", "videos"):
        try:
            results = with_retry(lambda f=filt: client.search(query, filter=f)) or []
        except Exception:  # noqa: BLE001
            logger.warning("alternate-version search (%s) failed for %r", filt, query)
            results = []
        for r in results:
            vid = r.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            album = r.get("album")
            is_video = filt == "videos" or r.get("resultType") == "video" \
                or r.get("videoType") == "MUSIC_VIDEO_TYPE_UGC"
            out.append({
                "videoId": vid,
                "title": r.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name")),
                "album": album.get("name") if isinstance(album, dict) else None,
                "album_browse": album.get("id") if isinstance(album, dict) else None,
                "duration": r.get("duration_seconds") or _parse_duration(r.get("duration")),
                "thumbnail": best_thumb(r.get("thumbnails")),
                "kind": "video" if is_video else "song",
            })
            if len(out) >= limit:
                return out
    return out


def add_tracks_to_playlist(store, playlist_id, tracks, client, now, after_video_id=None) -> dict:
    """Append the given tracks (full metadata dicts) to an existing playlist on YouTube, then seed
    the store directly from what we added (no racing read-back). When `after_video_id` is an existing
    track, the freshly-added tracks are then slotted just below it (preserving their order) rather than
    left at the end. Returns counts."""
    pl = store.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("can't add tracks to a system playlist")
    items = [t for t in tracks if t.get("videoId")]
    if not items:
        raise ValueError("no tracks to add")
    # the track currently after the anchor — captured before we append, so we know where to slot the
    # new tracks back to. None (anchor is last, or absent) means "leave them at the end".
    successor_vid = None
    if after_video_id:
        order = [t["video_id"] for t in store.playlist_tracks_detail(playlist_id)]
        if after_video_id in order:
            i = order.index(after_video_id)
            successor_vid = order[i + 1] if i + 1 < len(order) else None
    added, skipped = _add_items(client, pl.ytm_playlist_id, [t["videoId"] for t in items])
    skipped_set = set(skipped)
    existing = store.get_playlist_track_ids(playlist_id)
    new_ids, titles = [], []
    for t in items:
        if t["videoId"] in skipped_set:
            continue
        new_ids.append(store.upsert_track(t["videoId"], t.get("title", ""), t.get("artist"),
                                          t.get("album"), t.get("duration"), 1,
                                          None, None, t.get("album_browse"), t.get("thumbnail")))
        titles.append(t.get("title", ""))
    combined = list(dict.fromkeys(existing + new_ids))
    store.set_playlist_tracks(playlist_id, combined)
    store.set_playlist_track_count(playlist_id, len(combined), now)
    store.record_action(ADD_TRACKS,
                        json.dumps({"playlist": pl.title, "added": added, "titles": titles}),
                        "{}", "executed", "{}", now)
    # Move each new track to sit just before the anchor's old successor. Moving them in order before the
    # same successor preserves their order (each lands right after the previous one). Best-effort: the
    # add already succeeded, so a positioning hiccup just leaves a track at the end rather than failing.
    if successor_vid:
        for t in items:
            if t["videoId"] in skipped_set:
                continue
            try:
                reorder_track(store, playlist_id, t["videoId"], successor_vid, client, now)
            except Exception:  # noqa: BLE001 - positioning is best-effort
                logger.warning("could not slot %s below %s in playlist %s",
                               t["videoId"], after_video_id, playlist_id)
    return {"added": added, "skipped": len(skipped), "count": len(combined)}


def _set_video_ids(client, ytm_playlist_id) -> dict:
    """Fetch the playlist and map videoId -> setVideoId (the per-item handle YT needs to move/remove
    items). We don't persist setVideoIds (they're playlist-scoped and change), so we read them live."""
    detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
    out = {}
    for t in detail.get("tracks", []):
        vid, svid = t.get("videoId"), t.get("setVideoId")
        if vid and svid and vid not in out:
            out[vid] = svid
    return out


def rename_playlist(store, playlist_id, title, client, now) -> dict:
    """Rename a playlist on YouTube and in the store."""
    pl = store.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("system playlists can't be renamed")
    title = (title or "").strip()
    if not title:
        raise ValueError("name can't be empty")
    with_retry(lambda: client.edit_playlist(pl.ytm_playlist_id, title=title))
    store.set_playlist_title(playlist_id, title, now)
    store.record_action(RENAME_PLAYLIST,
                        json.dumps({"from": pl.title, "to": title}), "{}", "executed", "{}", now)
    return {"title": title}


def remove_track(store, playlist_id, video_id, client, now) -> dict:
    """Remove a single track from a real playlist (YouTube + store). Liked Music is handled upstream
    by LikedMusic.remove (an unlike), so by here a system playlist is always a hard error."""
    pl = store.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("can't remove tracks from a system playlist")
    svid = _set_video_ids(client, pl.ytm_playlist_id).get(video_id)
    if svid is None:
        raise ValueError("track not found on YouTube (already removed?)")
    with_retry(lambda: client.remove_playlist_items(
        pl.ytm_playlist_id, [{"videoId": video_id, "setVideoId": svid}]))
    moved_tid = store.track_ids_for_videos([video_id]).get(video_id)
    ids = [i for i in store.get_playlist_track_ids(playlist_id) if i != moved_tid]
    store.set_playlist_tracks(playlist_id, ids)
    store.set_playlist_track_count(playlist_id, len(ids), now)
    store.record_action(REMOVE_TRACK,
                        json.dumps({"playlist": pl.title, "video_id": video_id}),
                        "{}", "executed", "{}", now)
    return {"count": len(ids)}


def reorder_track(store, playlist_id, video_id, before_video_id, client, now) -> dict:
    """Move `video_id` so it sits just before `before_video_id` (or to the end if that's empty),
    on YouTube and in the store. One move per call — matches a single drag-and-drop."""
    pl = store.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("can't reorder a system playlist")
    if not video_id or video_id == before_video_id:
        return {"ok": True}
    svids = _set_video_ids(client, pl.ytm_playlist_id)
    moved = svids.get(video_id)
    if moved is None:
        raise ValueError("track not found on YouTube")
    successor = svids.get(before_video_id) if before_video_id else None
    # moveItem=(moved, successor) places `moved` before `successor`; a bare setVideoId moves to the end
    with_retry(lambda: client.edit_playlist(
        pl.ytm_playlist_id, moveItem=(moved, successor) if successor else moved))
    tmap = store.track_ids_for_videos([video_id] + ([before_video_id] if before_video_id else []))
    moved_tid = tmap.get(video_id)
    ids = [i for i in store.get_playlist_track_ids(playlist_id) if i != moved_tid]
    before_tid = tmap.get(before_video_id) if before_video_id else None
    if before_tid in ids:
        ids.insert(ids.index(before_tid), moved_tid)
    else:
        ids.append(moved_tid)
    store.set_playlist_tracks(playlist_id, ids)
    return {"ok": True}


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

def _pull_recreated(store, client, identity_id, new_pid, title, now):
    """Bring a just-recreated playlist into the local store so it appears without a full re-sync."""
    if store is None or now is None:
        return
    try:
        from yt_playlist import sync as _sync   # local import avoids an import cycle
        _sync.refresh_playlist(store, identity_id, client, new_pid, title or "Restored", now)
    except Exception:  # noqa: BLE001
        logger.warning("undo: recreated %s but couldn't pull it into the store (re-sync to see it)", new_pid)

def _recreate_from_backup(clients, backup_path, store=None, now=None):
    """Recreate a deleted playlist from its JSON backup, under the identity it belonged to."""
    payload = json.loads(Path(backup_path).read_text())
    identity_id = payload.get("identity_id")
    if identity_id is None:
        raise ValueError("backup has no identity; cannot recreate")
    client = _client_for(clients, identity_id)
    new_pid = client.create_playlist(payload["title"], "Recreated by TuneConsole undo")
    vids = [t["video_id"] for t in payload.get("tracks", []) if t.get("video_id")]
    if vids:
        client.add_playlist_items(new_pid, vids)
    logger.warning("undo: recreated %r as %s", payload.get("title"), new_pid)
    _pull_recreated(store, client, identity_id, new_pid, payload.get("title"), now)
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
        if action.kind in (MOVE_IDENTITY, COPY_PLAYLIST) and undo.get("new_ytm") is not None:
            try:                                          # remove the copy this action created
                _client_for(clients, undo["target_identity"]).delete_playlist(undo["new_ytm"])
                if action.kind == COPY_PLAYLIST:          # also drop its local row immediately
                    doomed = next((p for p in store.get_playlists()
                                   if p.ytm_playlist_id == undo["new_ytm"]), None)
                    if doomed is not None:
                        store.remove_playlist(doomed.id)
            except Exception:  # noqa: BLE001
                logger.warning("undo: could not delete recreated copy %s", undo.get("new_ytm"))
        backups = list(undo.get("backups", []))
        if undo.get("backup"):
            backups.append(undo["backup"])
        for bp in backups:
            _recreate_from_backup(clients, bp, store, now)
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
        new_pid = sclient.create_playlist(payload["title"], "Recreated by TuneConsole undo")
        vids = [t["video_id"] for t in payload.get("tracks", []) if t.get("video_id")]
        if vids:
            sclient.add_playlist_items(new_pid, vids)
        logger.warning("undo: recreated source playlist %r as %s", payload.get("title"), new_pid)
        _pull_recreated(store, sclient, identity_id, new_pid, payload.get("title"), now)
    store.record_action(UNDO, json.dumps({"undid": action_id}), "{}", "executed", "{}", now)
    store.update_action(action_id, "undone", now)
