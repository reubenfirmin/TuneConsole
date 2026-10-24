"""Generated playlist creation, quarantine, recipe materialization and unattended collection."""
import json
import logging

from yt_playlist.library.playlist_backups import backup_playlist
from yt_playlist.library.playlist_catalog import fetch_song_duration
from yt_playlist.library.playlist_deletion import delete_backed_up, resume_pending_deletion
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, content_hash, versioned_title
from yt_playlist.library.playlist_writes import (
    account_additions, add_items as _add_items, retain_addition_snapshot)
from yt_playlist.library.undo_capture import capture_copy
from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.util.action_kinds import COPY_PLAYLIST, GC_GENERATED

logger = logging.getLogger(__name__)
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
    action, exactly like a manual delete, just automatic. Returns the collected playlists.
    """
    from yt_playlist.rec import rec_params
    if grace_days is None:
        grace_days = rec_params.get_param(store, "generated_gc_days")
    grace_s = grace_days * 86400.0
    clients = clients or {}
    recency = store.charts.get_playlist_track_recency()         # pid -> [per-track last-played ts | None, ...]
    collected = []
    for pl in store.playlists.get_playlists():
        if not store.playlists.is_quarantined_playlist(pl.scope_ref):
            continue
        created = pl.first_seen or now
        if now - created < grace_s:                      # still inside its grace window, leave it
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
            resumed = resume_pending_deletion(store, pl, client, now)
            if resumed is not None:
                collected.append({"ytm": pl.ytm_playlist_id, "title": pl.title, "backup": resumed})
                continue
            backup_path = backup_playlist(store, pl.id, now, client)
            logger.warning("GC: deleting unplayed generated playlist %s (backup at %s)",
                           pl.ytm_playlist_id, backup_path)
            delete_backed_up(store, pl, client, backup_path, now, kind=GC_GENERATED)
            collected.append({"ytm": pl.ytm_playlist_id, "title": pl.title, "backup": backup_path})
        except Exception:  # noqa: BLE001 - one playlist's failure must not stop the sweep
            logger.warning("GC: could not delete generated playlist %s", pl.ytm_playlist_id,
                           exc_info=True)
    return collected

def create_generated_playlist(store, title, tracks, client, now, identity_id=None, group=None,
                              recipe=None) -> dict:
    """Create a NEW playlist on YouTube from recommendation tracks (dicts with video_id + metadata),
    record its generated origin and quarantine until explicit adoption, and optimistically
    materialize it into the store so it shows up in the Playlists tab right away (a later sync
    reconciles canonical order/metadata). Non-destructive: nothing existing is touched.

    Quarantine makes the optimistic insert safe even for unowned tracks (e.g. 'fresh songs' not yet in
    your library): the playlist is excluded from playlist-level signals, and its unplayed-only tracks
    are excluded from the embedding baskets until you explicitly adopt the playlist."""
    from yt_playlist.rec import rec_params
    group = group or GENERATED_GROUP
    uniq, seen = [], set()
    for t in tracks:
        v = (t or {}).get("video_id")
        if v and v not in seen:
            seen.add(v)
            uniq.append(t)
    if not uniq:
        raise ValueError("no tracks to add")
    # Resolve times before any remote writes: Fresh discoveries often have no stored duration,
    # and finding a two-hour concert after adding it is too late (#44). Keep these copies so local
    # materialization reuses each result, including unknowns, without another lookup (#26).
    max_duration = rec_params.max_track_duration_s(store)
    eligible = []
    for t in uniq:
        dur = t.get("duration")
        if dur is None:
            dur = store.tracks.known_duration(t.get("title") or "", t.get("artist") or "")
        if dur is None:
            dur = fetch_song_duration(client, t["video_id"])
        if dur is None or dur <= max_duration:
            eligible.append({**t, "duration": dur})
    excluded = len(uniq) - len(eligible)
    uniq = eligible
    if not uniq:
        raise ValueError("No tracks are within the maximum track length set in Taste Model.")
    title = (title or "Generated playlist").strip()
    if recipe is not None:                                # recipe-driven: version the title
        title = versioned_title(store, title)
        # NB: the mix is already DJ-ordered in the proto card (journeys.journey_order in home._carded),
        # so we persist the posted rows in the order the user actually saw: WYSIWYG. Re-ordering here
        # would diverge from the preview (the shuffle is seed-stable but its input differs once rows
        # are pruned). Pruning just drops rows; the survivors keep their already-good spacing.
    new_pid = client.create_playlist(title, GENERATED_DESCRIPTION)
    store.playlists.mark_generated_playlist(new_pid)
    store.playlists.set_playlist_group(new_pid, group)
    addition = account_additions(_add_items, client, new_pid, [t["video_id"] for t in uniq], prior_tracks=[])
    added, skipped = addition.added, addition.skipped
    db_pid = None
    if identity_id is not None:                          # optimistic local materialization
        confirmed = set(addition.confirmed)
        track_ids, keys = [], []
        for t in uniq:
            if t["video_id"] not in confirmed:
                continue
            ti, ar = t.get("title") or "", t.get("artist") or ""
            track_ids.append(store.tracks.upsert_track(
                t["video_id"], ti, ar, t.get("album") or "", t["duration"], thumbnail=t.get("thumbnail")))
        keys = [store.tracks.catalog_subject(tid)["identity_key"] for tid in track_ids]
        db_pid = store.playlists.upsert_playlist(identity_id, new_pid, title, len(track_ids),
                                       content_hash(list(dict.fromkeys(keys))), now, created_at=now)
        store.playlists.set_playlist_tracks(db_pid, track_ids)
        store.playlists.ensure_playlist_thumbnail(db_pid)
        retain_addition_snapshot(store, db_pid, addition, now)
    store.actions.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": "recommendations", "group": group,
                                    "added": added, "skipped": len(skipped) + excluded,
                                    "addition": addition.diagnostics(), "partial": bool(addition.failure)}),
                        "{}", "executed", json.dumps(capture_copy(client, new_pid, identity_id,
                            [t["video_id"] for t in uniq if t["video_id"] not in skipped],
                            title=title, description=GENERATED_DESCRIPTION, addition=addition)), now)
    if recipe is not None:
        store.playlist_recipes.set_recipe(new_pid, recipe, now)           # remember exactly how this one was made
    addition.finish()
    return {"new_ytm": new_pid, "pid": db_pid, "title": title, "added": added,
            "skipped": len(skipped) + excluded}

