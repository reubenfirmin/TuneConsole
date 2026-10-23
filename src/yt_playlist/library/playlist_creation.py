"""Create ordinary user playlists and copy existing library songs, retaining partial progress."""
import json
import logging

from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.library.executor import add_tracks_to_playlist
from yt_playlist.library.playlist_metadata import content_hash
from yt_playlist.library.playlist_writes import (
    AdditionOutcome, PlaylistAddUncertain, account_additions, add_items as _add_items,
    retain_addition_snapshot)
from yt_playlist.library.undo_capture import capture_copy
from yt_playlist.util.action_kinds import COPY_INTO, COPY_PLAYLIST
from yt_playlist.util.retry import with_retry
from yt_playlist.util.thumbnails import best_thumb

logger = logging.getLogger(__name__)


def copy_playlist(store, playlist_ids, new_name, client, now) -> dict:
    """Copy one or more playlists into a NEW playlist (non-destructive). With several, it's a
    copy+merge: their tracks are unioned (de-duped, order preserved). Pulled into the store."""
    pls = [p for p in (store.playlists.get_playlist(i) for i in playlist_ids) if p is not None]
    if not pls:
        raise ValueError("no playlists to copy")
    vids, keys, seen = [], {}, set()
    for pl in pls:
        for (k, v, t, a, d, av) in store.playlists.get_playlist_tracks_with_meta(pl.id):
            if v and v not in seen:
                seen.add(v)
                vids.append(v)
                keys[v] = k
    title = (new_name or "").strip() or (f"{pls[0].title} (copy)" if len(pls) == 1 else "Combined playlist")
    identity = pls[0].identity_id
    new_pid = client.create_playlist(title, "Copied by TuneConsole")
    addition = account_additions(_add_items, client, new_pid, vids, prior_tracks=[])
    added, skipped = addition.added, addition.skipped
    store.actions.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": ", ".join(p.title for p in pls),
                                    "added": added, "skipped": len(skipped),
                                    "addition": addition.diagnostics(), "partial": bool(addition.failure)}),
                        "{}", "executed",
                        json.dumps(capture_copy(client, new_pid, identity,
                                              [v for v in vids if v not in skipped],
                                              title=title, description="Copied by TuneConsole",
                                              addition=addition)), now)
    # Seed the store from the tracks we know we added. A read-back from YouTube here would race
    # its indexing lag and often returns an empty playlist; the sources are already in our store,
    # so we map them directly. A later full sync reconciles canonical order/metadata.
    added_vids = list(addition.confirmed)
    tid_by_vid = store.tracks.track_ids_for_videos(added_vids)
    track_ids = [tid_by_vid[v] for v in added_vids if v in tid_by_vid]
    track_keys = list(dict.fromkeys(keys[v] for v in added_vids if v in keys))
    db_pid = store.playlists.upsert_playlist(identity, new_pid, title, len(track_ids), content_hash(track_keys), now,
                                   created_at=now)
    store.playlists.set_playlist_tracks(db_pid, track_ids)
    store.playlists.ensure_playlist_thumbnail(db_pid)
    retain_addition_snapshot(store, db_pid, addition, now)
    addition.finish()
    return {"new_ytm": new_pid, "title": title, "added": added, "skipped": len(skipped),
            "skipped_video_ids": list(skipped), "from": len(pls)}

def create_playlist_from_song(store, name, track, client, now, identity_id) -> dict:
    """Create an ordinary user playlist, seeding it through the shared track-add operation.

    Materialize the new playlist before adding so a rejected song still leaves a discoverable
    playlist and the caller can report the partial result without creating another on retry.
    """
    title = (name or "").strip()
    if not title or not track.get("videoId"):
        raise ValueError("Choose a song and give the playlist a name.")
    new_pid = client.create_playlist(title, "Created by TuneConsole")
    # Creation is already a remote side effect. Retain its destination before any local
    # materialization or seed work can fail; pending actions cannot be undone mid-write.
    action_id = store.actions.record_action(
        COPY_PLAYLIST, json.dumps({"title": title, "source": "song", "added": 0, "partial": True}),
        "{}", "pending", json.dumps({"new_ytm": new_pid, "target_identity": identity_id,
                                     "post": None}), now)
    addition = None
    try:
        db_pid = store.playlists.upsert_playlist(
            identity_id, new_pid, title, 0, content_hash([]), now, created_at=now)
        # A playlist just created by this operation has a known empty baseline, even while
        # YouTube's first readable page is still propagating. Existing destinations still read.
        result = add_tracks_to_playlist(store, db_pid, [track], client, now, prior_tracks=[])
    except PlaylistAddUncertain as exc:
        addition = AdditionOutcome(exc.confirmed, exc.skipped, exc)
        result = {"added": addition.added}
    except Exception as exc:  # noqa: BLE001 - retain the destination after any post-creation failure
        logger.warning("song playlist %s created but seed bookkeeping failed (%s)",
                       new_pid, type(exc).__name__)
        failure = PlaylistAddUncertain(new_pid, [], [], [track["videoId"]])
        addition = AdditionOutcome((), (), failure)
        result = {"added": 0}
    store.actions.update_action(
        action_id, "executed", now,
        json.dumps(capture_copy(client, new_pid, identity_id,
                             [track["videoId"]] if result["added"] else [],
                             title=title, description="Created by TuneConsole", addition=addition)),
        params_json=json.dumps({"title": title, "source": "song", "added": result["added"],
                                "partial": bool(addition),
                                "addition": addition.diagnostics() if addition else None}))
    if addition:
        addition.finish()
    return {**result, "db_pid": db_pid, "new_ytm": new_pid, "title": title}


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
    addition = account_additions(_add_items, client, new_pid, vids, prior_tracks=[])
    added, skipped = addition.added, addition.skipped
    confirmed = set(addition.confirmed)
    thumb = best_thumb(a.get("thumbnails"))
    track_ids, keys = [], []
    for v in vids:
        if v not in confirmed:
            continue
        ti, ar, al = meta[v]
        track_ids.append(store.tracks.upsert_track(v, ti, ar, al, None, thumbnail=thumb))
    keys = [store.tracks.catalog_subject(tid)["identity_key"] for tid in track_ids]
    db_pid = store.playlists.upsert_playlist(identity_id, new_pid, title, len(track_ids),
                                   content_hash(list(dict.fromkeys(keys))), now, created_at=now)
    store.playlists.set_playlist_tracks(db_pid, track_ids)
    store.playlists.ensure_playlist_thumbnail(db_pid)
    retain_addition_snapshot(store, db_pid, addition, now)
    store.actions.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": a.get("title") or "album",
                                    "added": added, "skipped": len(skipped),
                                    "addition": addition.diagnostics(), "partial": bool(addition.failure)}),
                        "{}", "executed", json.dumps(capture_copy(client, new_pid, identity_id,
                            [v for v in vids if v not in skipped], title=title,
                            description=f"Created from “{a.get('title')}” by TuneConsole",
                            addition=addition)), now)
    addition.finish()
    return {"new_ytm": new_pid, "db_pid": db_pid, "title": title, "added": added, "skipped": len(skipped)}

def copy_into_playlist(store, source_ids, target_id, client, now) -> dict:
    """Copy the union of tracks from one or more source playlists INTO an existing target playlist
    (non-destructive append). Songs already in the target (by identity_key) are skipped, so it's
    safe to re-run. The source playlists are left untouched."""
    target = store.playlists.get_playlist(target_id)
    if target is None:
        raise ValueError("target playlist no longer exists")
    if target.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError("can't copy into a system playlist")
    sources = [p for p in (store.playlists.get_playlist(i) for i in source_ids) if p is not None and p.id != target_id]
    if not sources:
        raise ValueError("no source playlists to copy")
    # Add with the TARGET's client, so every source video must belong to the target's account. A
    # cross-account copy would feed account A's videoIds to account B's client. Region/upload-scoped
    # ids silently 400 and land in `skipped`, leaving the destination short. Require one account.
    if any(p.identity_id != target.identity_id for p in sources):
        raise ValueError("can only copy into a playlist on the same account")
    have = set(store.playlists.get_playlist_track_keys(target_id))   # songs already in the target
    vids, seen = [], set()
    for pl in sources:
        for (k, v, t, a, d, av) in store.playlists.get_playlist_tracks_with_meta(pl.id):
            if v and v not in seen and k not in have:
                seen.add(v)
                have.add(k)                                # also de-dupe across sources by song
                vids.append(v)
    addition = account_additions(_add_items, client, target.ytm_playlist_id, vids)
    added, skipped = addition.added, addition.skipped
    added_vids = list(addition.confirmed)
    tid_by_vid = store.tracks.track_ids_for_videos(added_vids)
    new_ids = [tid_by_vid[v] for v in added_vids if v in tid_by_vid]
    combined = list(dict.fromkeys(store.playlists.get_playlist_track_ids(target_id) + new_ids))
    store.playlists.set_playlist_tracks(target_id, combined)
    store.playlists.set_playlist_track_count(target_id, len(combined), now)
    store.playlists.ensure_playlist_thumbnail(target_id)
    retain_addition_snapshot(store, target_id, addition, now)
    store.actions.record_action(COPY_INTO,
                        json.dumps({"target": target.title, "added": added, "skipped": len(skipped),
                                    "addition": addition.diagnostics(), "partial": bool(addition.failure),
                                    "source": ", ".join(p.title for p in sources)}),
                        "{}", "executed", json.dumps({"addition": addition.diagnostics(),
                            "snapshot": addition.failure.snapshot if addition.failure else None}), now)
    addition.finish()
    return {"target_ytm": target.ytm_playlist_id, "title": target.title, "added": added,
            "skipped": len(skipped), "skipped_video_ids": list(skipped),
            "from": len(sources), "count": len(combined)}

