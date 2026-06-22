import hashlib
import logging
from yt_playlist.util.matching import identity_key, track_artist, track_album
from yt_playlist.util.retry import with_retry
from yt_playlist.util.thumbnails import best_thumb

logger = logging.getLogger(__name__)

def content_hash(track_keys) -> str:
    joined = "\n".join(sorted(track_keys))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()

_artist = track_artist
_album = track_album

def _artist_id(t):
    arts = t.get("artists") or []
    return arts[0].get("id") if arts and isinstance(arts[0], dict) else None

def _album_id(t):
    alb = t.get("album")
    return alb.get("id") if isinstance(alb, dict) else None

def _emit(on_progress, type, text, **extra):
    """Report a progress event to an optional callback (used to stream sync to the browser)."""
    if on_progress is not None:
        on_progress({"type": type, "text": text, **extra})

def _is_auth_error(e) -> bool:
    s = str(e).lower()
    return "401" in s or "403" in s or "unauthorized" in s

def sync_identity(store, identity_id, client, now, on_progress=None, label=None,
                  on_auth_expired=None, on_auth_ok=None) -> None:
    label = label or f"identity {identity_id}"
    logger.info("syncing identity %s", identity_id)
    _emit(on_progress, "info", f"{label}: fetching playlists…")
    try:
        playlists = with_retry(lambda: client.get_library_playlists(limit=None))
    except Exception as e:  # noqa: BLE001 - an expired session shouldn't abort the whole sync
        if _is_auth_error(e):
            logger.warning("auth expired for identity %s: %s", identity_id, e)
            if on_auth_expired:
                on_auth_expired(identity_id, label)
            _emit(on_progress, "auth_expired",
                  f"{label}: YouTube session expired — re-authenticate", label=label)
            return
        raise
    _emit(on_progress, "info", f"{label}: {len(playlists)} playlists", count=len(playlists))
    total = len(playlists)
    seen_ytm = set()
    rated: dict[str, str] = {}                  # identity_key -> likeStatus; DISLIKE wins on conflict
    for i, pl in enumerate(playlists, 1):
        pid = pl["playlistId"]
        seen_ytm.add(pid)  # mark seen before fetch so a transient read failure doesn't prune it
        try:
            detail = with_retry(lambda: client.get_playlist(pid, limit=None))
        except Exception as e:  # noqa: BLE001 - one bad/just-deleted playlist must not abort the sync
            logger.warning("skipping playlist %s: %s", pid, e)
            _emit(on_progress, "info", f"{label} › skipped {pl.get('title', '')} (couldn't read)")
            continue
        track_ids, keys = [], []
        for t in detail.get("tracks", []):
            tid = store.upsert_track(t.get("videoId"), t.get("title", ""), _artist(t),
                                     _album(t), t.get("duration_seconds"), t.get("isAvailable"),
                                     t.get("videoType"), _artist_id(t), _album_id(t), best_thumb(t.get("thumbnails")))
            track_ids.append(tid)
            keys.append(identity_key(t.get("title", ""), _artist(t)))
            status = t.get("likeStatus")
            if status:
                rk = identity_key(t.get("title", ""), _artist(t))
                if rated.get(rk) != "DISLIKE":
                    rated[rk] = status
        track_ids = list(dict.fromkeys(track_ids))   # de-dupe (YouTube can repeat a video; see set_playlist_tracks)
        keys = list(dict.fromkeys(keys))
        chash = content_hash(keys)
        db_pid = store.upsert_playlist(identity_id, pid, pl.get("title", ""),
                                       len(track_ids), chash, now, best_thumb(pl.get("thumbnails")))
        store.set_playlist_tracks(db_pid, track_ids)
        _emit(on_progress, "step", f"{label} › {pl.get('title', '')} ({len(track_ids)} tracks)",
              i=i, total=total)

    # Prune playlists no longer in this identity's remote library (deleted elsewhere / stale rows) —
    # but ONLY when the fetch actually returned a library. An empty result is almost always a
    # transient or session glitch (it doesn't always surface as a 401/403), and pruning on it would
    # destructively wipe every playlist for the identity. Sync must only *update* on success, never
    # clear on a non-result. A later, real sync re-adds anything genuinely missing.
    auth_bad = False
    if playlists:
        stale = [p for p in store.get_playlists()
                 if p.identity_id == identity_id and p.ytm_playlist_id not in seen_ytm]
        for p in stale:
            store.remove_playlist(p.id)
        if stale:
            _emit(on_progress, "info", f"{label}: removed {len(stale)} playlist(s) no longer present")
    else:
        # An empty library on a configured identity is, in practice, a broken/expired session — some
        # endpoints return [] instead of raising a 401 (exactly what happened to the master account
        # while the brand account got a clean 401). Treat it the SAME: flag for re-auth, never prune.
        auth_bad = True
        logger.warning("identity %s returned no playlists — flagging for re-auth, keeping existing",
                       identity_id)
        if on_auth_expired:
            on_auth_expired(identity_id, label)
        _emit(on_progress, "auth_expired",
              f"{label}: returned no playlists — session may have expired, re-authenticate", label=label)

    from yt_playlist.rec import recommend            # local import avoids any import cycle
    recommend.apply_dislikes(store, rated, now)
    _emit(on_progress, "info", f"{label}: fetching history…")
    try:  # history is best-effort (powers stale detection); never let it fail the whole sync
        history = with_retry(lambda: client.get_history())
        hist_keys = [identity_key(t.get("title", ""), _artist(t)) for t in history]
        store.add_history_snapshot(identity_id, now, hist_keys)
    except Exception as e:  # noqa: BLE001
        logger.warning("history fetch failed for %s: %s", identity_id, e)
        _emit(on_progress, "info", f"{label}: history unavailable (skipped)")
    if not auth_bad:
        if on_auth_ok:
            on_auth_ok(identity_id)   # genuine success -> clear any "expired" flag
        logger.info("synced identity %s: %d playlists", identity_id, len(playlists))
        _emit(on_progress, "done", f"{label}: done ({len(playlists)} playlists)")

def refresh_playlist(store, identity_id, client, ytm_playlist_id, title, now) -> None:
    """Re-fetch a single playlist's tracks into the store — fast post-merge refresh (no full sync)."""
    detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
    track_ids, keys = [], []
    for t in detail.get("tracks", []):
        tid = store.upsert_track(t.get("videoId"), t.get("title", ""), _artist(t),
                                 _album(t), t.get("duration_seconds"), t.get("isAvailable"),
                                 t.get("videoType"), _artist_id(t), _album_id(t), best_thumb(t.get("thumbnails")))
        track_ids.append(tid)
        keys.append(identity_key(t.get("title", ""), _artist(t)))
    track_ids = list(dict.fromkeys(track_ids))   # de-dupe repeated videos
    keys = list(dict.fromkeys(keys))
    db_pid = store.upsert_playlist(identity_id, ytm_playlist_id, title,
                                   len(track_ids), content_hash(keys), now)
    store.set_playlist_tracks(db_pid, track_ids)

def sync_plays_identity(store, identity_id, client, now, on_progress=None, label=None,
                        on_auth_expired=None, on_auth_ok=None) -> None:
    """Fast, lightweight sync: pull only this identity's likes (the Liked Music playlist) and new
    plays (listening history). Deliberately skips the full-library enumeration, pruning and saved-
    album work that make `sync_identity` slow — meant to be run often between full syncs."""
    label = label or f"identity {identity_id}"
    auth_bad = False

    # Likes: the Liked Music (LM) system playlist's membership *is* your likes, so refreshing that one
    # playlist captures likes/unlikes made outside the app. Reuse the existing single-playlist refresh.
    existing = next((p for p in store.get_playlists()
                     if p.identity_id == identity_id and p.ytm_playlist_id == "LM"), None)
    _emit(on_progress, "info", f"{label}: refreshing Liked Music…")
    try:
        refresh_playlist(store, identity_id, client, "LM", existing.title if existing else "Liked Music", now)
    except Exception as e:  # noqa: BLE001 - a likes failure shouldn't abort the (best-effort) plays sync
        if _is_auth_error(e):
            auth_bad = True
            if on_auth_expired:
                on_auth_expired(identity_id, label)
            _emit(on_progress, "auth_expired",
                  f"{label}: YouTube session expired — re-authenticate", label=label)
        else:
            logger.warning("liked-music refresh failed for %s: %s", identity_id, e)
            _emit(on_progress, "info", f"{label}: Liked Music unavailable (skipped)")

    # Plays: snapshot the listening history (the "new plays").
    _emit(on_progress, "info", f"{label}: fetching history…")
    try:
        history = with_retry(lambda: client.get_history())
        hist_keys = [identity_key(t.get("title", ""), _artist(t)) for t in history]
        store.add_history_snapshot(identity_id, now, hist_keys)
    except Exception as e:  # noqa: BLE001
        logger.warning("history fetch failed for %s: %s", identity_id, e)
        _emit(on_progress, "info", f"{label}: history unavailable (skipped)")

    if not auth_bad:
        if on_auth_ok:
            on_auth_ok(identity_id)   # genuine success -> clear any "expired" flag
        _emit(on_progress, "done", f"{label}: plays synced")

def sync_plays_all(store, clients, now, on_progress=None, on_auth_expired=None, on_auth_ok=None) -> None:
    """Fast plays/likes sync across all identities. Records its own `last_plays_sync_at` marker and
    leaves `last_sync_at` (which drives the full-sync nudge) alone."""
    labels = {idn.id: idn.label for idn in store.get_identities()}
    for identity_id, client in clients.items():
        sync_plays_identity(store, identity_id, client, now,
                            on_progress=on_progress, label=labels.get(identity_id),
                            on_auth_expired=on_auth_expired, on_auth_ok=on_auth_ok)
    store.set_setting("last_plays_sync_at", str(now))
    _emit(on_progress, "done", "plays synced", final=True)

def _sync_saved_albums(store, clients, on_progress) -> None:
    """Pull the albums saved in each account's library and store them (best-effort)."""
    saved = {}
    for client in clients.values():
        try:
            for a in with_retry(lambda: client.get_library_albums(limit=500)) or []:
                bid = a.get("browseId")
                if not bid:
                    continue
                saved[bid] = {"browse": bid, "title": a.get("title"),
                              "artist": ", ".join(x.get("name", "") for x in (a.get("artists") or [])),
                              "year": a.get("year"), "type": a.get("type"),
                              "thumbnail": best_thumb(a.get("thumbnails"))}
        except Exception as e:  # noqa: BLE001
            logger.warning("saved-albums fetch failed: %s", e)
    store.replace_saved_albums(list(saved.values()))
    _emit(on_progress, "info", f"saved albums: {len(saved)}")
    _materialize_album_tracks(store, clients, saved, on_progress)


def _materialize_album_tracks(store, clients, saved, on_progress) -> None:
    """Fold each saved album's TRACKS into the library so they count in the taste corpus (the model
    is built from your tracks; metadata alone doesn't). Incremental: only albums not already
    materialized are fetched (their tracks carry album_browse_id, so we skip them next time)."""
    client = next(iter(clients.values()), None)
    if client is None:
        return
    todo = [bid for bid in saved if bid not in store.materialized_album_ids()]
    if not todo:
        return
    # Each album is a separate get_album network call, so stream one step per album (named, with a
    # running counter) instead of going silent for the whole batch — 258 of these is a long wait.
    _emit(on_progress, "info", f"folding in {len(todo)} saved album(s)…", count=len(todo))
    added = 0
    for i, bid in enumerate(todo, 1):
        meta = saved[bid]
        _emit(on_progress, "step",
              f"albums › {i}/{len(todo)} {meta.get('title') or '(album)'} — {meta.get('artist') or '?'}",
              count=len(todo), index=i)
        try:
            album = with_retry(lambda: client.get_album(bid))
        except Exception as e:  # noqa: BLE001 - one album's failure shouldn't abort the rest
            logger.warning("album-tracks fetch failed for %s: %s", bid, e)
            continue
        for t in (album or {}).get("tracks") or []:
            if not t.get("title"):
                continue
            artist = ", ".join(x.get("name", "") for x in (t.get("artists") or [])) or meta["artist"]
            store.upsert_track(t.get("videoId"), t.get("title"), artist, meta["title"], None,
                               album_browse_id=bid, thumbnail=meta["thumbnail"])
            added += 1
    _emit(on_progress, "info", f"album tracks folded in: {added} from {len(todo)} album(s)")

def sync_all(store, clients, now, on_progress=None, on_auth_expired=None, on_auth_ok=None) -> None:
    labels = {idn.id: idn.label for idn in store.get_identities()}
    for identity_id, client in clients.items():
        sync_identity(store, identity_id, client, now,
                      on_progress=on_progress, label=labels.get(identity_id),
                      on_auth_expired=on_auth_expired, on_auth_ok=on_auth_ok)
    _sync_saved_albums(store, clients, on_progress)
    store.set_setting("last_sync_at", str(now))   # drives the Home "Time to sync" nudge
    # the recommendation model is rebuilt by the decoupled RecWorker (triggered from the sync
    # route), so a burst of syncs coalesces into one rebuild instead of blocking each sync.
    _emit(on_progress, "done", "sync complete", final=True)
