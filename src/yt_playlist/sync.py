import hashlib
import logging
from yt_playlist.matching import identity_key, track_artist, track_album
from yt_playlist.retry import with_retry
from yt_playlist.thumbnails import best_thumb

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
            _emit(on_progress, "err", f"{label}: YouTube session expired — re-authenticate")
            return
        raise
    _emit(on_progress, "info", f"{label}: {len(playlists)} playlists", count=len(playlists))
    total = len(playlists)
    seen_ytm = set()
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
        track_ids = list(dict.fromkeys(track_ids))   # de-dupe (YouTube can repeat a video; see set_playlist_tracks)
        keys = list(dict.fromkeys(keys))
        chash = content_hash(keys)
        db_pid = store.upsert_playlist(identity_id, pid, pl.get("title", ""),
                                       len(track_ids), chash, now, best_thumb(pl.get("thumbnails")))
        store.set_playlist_tracks(db_pid, track_ids)
        _emit(on_progress, "step", f"{label} › {pl.get('title', '')} ({len(track_ids)} tracks)",
              i=i, total=total)

    # prune playlists that are no longer in this identity's remote library (deleted elsewhere /
    # stale rows). Local-only removal: a later sync re-adds any that reappear remotely.
    stale = [p for p in store.get_playlists()
             if p.identity_id == identity_id and p.ytm_playlist_id not in seen_ytm]
    for p in stale:
        store.remove_playlist(p.id)
    if stale:
        _emit(on_progress, "info", f"{label}: removed {len(stale)} playlist(s) no longer present")

    _emit(on_progress, "info", f"{label}: fetching history…")
    try:  # history is best-effort (powers stale detection); never let it fail the whole sync
        history = with_retry(lambda: client.get_history())
        hist_keys = [identity_key(t.get("title", ""), _artist(t)) for t in history]
        store.add_history_snapshot(identity_id, now, hist_keys)
    except Exception as e:  # noqa: BLE001
        logger.warning("history fetch failed for %s: %s", identity_id, e)
        _emit(on_progress, "info", f"{label}: history unavailable (skipped)")
    if on_auth_ok:
        on_auth_ok(identity_id)   # session is good -> clear any "expired" flag
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

def sync_all(store, clients, now, on_progress=None, on_auth_expired=None, on_auth_ok=None) -> None:
    labels = {idn.id: idn.label for idn in store.get_identities()}
    for identity_id, client in clients.items():
        sync_identity(store, identity_id, client, now,
                      on_progress=on_progress, label=labels.get(identity_id),
                      on_auth_expired=on_auth_expired, on_auth_ok=on_auth_ok)
    _sync_saved_albums(store, clients, on_progress)
    _emit(on_progress, "done", "sync complete", final=True)
