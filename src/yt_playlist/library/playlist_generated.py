"""Generated playlist creation, quarantine, recipe materialization and unattended collection."""
import json
import logging
from types import SimpleNamespace

from yt_playlist.core.activity import WorkClosing
from yt_playlist.library.playlist_backups import backup_playlist
from yt_playlist.library.action_claim import claim_deletion_target
from yt_playlist.library.playlist_catalog import fetch_song_duration
from yt_playlist.library.playlist_deletion import delete_backed_up, resume_gc_deletion
from yt_playlist.library import playlist_gc_retry
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, content_hash, versioned_title
from yt_playlist.library.playlist_writes import (
    account_additions, add_items as _add_items, retain_addition_snapshot)
from yt_playlist.library.undo_capture import capture_copy
from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.util.action_kinds import COPY_PLAYLIST, GC_GENERATED

logger = logging.getLogger(__name__)
GC_PLAYED_FRACTION = 0.5


def _gc_eligible(store, playlist, recency, now, grace_s):
    if not store.playlists.is_quarantined_playlist(playlist.scope_ref):
        return False
    created = playlist.first_seen or now
    if now - created < grace_s:
        return False
    lasts = recency.get(playlist.id) or []
    played = sum(1 for at in lasts if at is not None and at >= created)
    return not lasts or played / len(lasts) < GC_PLAYED_FRACTION


def _gc_eligible_now(store, playlist, now, grace_s):
    recency = store.charts.get_playlist_track_recency(
        include_generated=True, playlist_id=playlist.id)
    return _gc_eligible(store, playlist, recency, now, grace_s)


def _gc_targets(store, recency, now, grace_s):
    """Keep saved attempts discoverable even after sync prunes their local rows."""
    playlists = {(pl.identity_id, pl.ytm_playlist_id): pl for pl in store.playlists.get_playlists()}
    targets = {target: pl for target, pl in playlists.items()
               if _gc_eligible(store, pl, recency, now, grace_s)}
    retries = playlist_gc_retry.states(store)
    for state in retries.values():
        target = (state["identity"], state["ytm"])
        if target in targets:
            continue
        # Recheck eligibility and saved intent under the target claim. A missing
        # library row is not proof that the earlier remote deletion succeeded.
        targets[target] = playlists.get(target) or SimpleNamespace(
            id=None, identity_id=target[0], ytm_playlist_id=target[1], title=target[1])
    return targets.values()


def gc_generated_playlists(store, clients, now, grace_days=None, *, retry_only=False) -> list[dict]:
    """Garbage-collect generated playlists that have gone (mostly) unplayed past their grace window.

    A generated playlist (group == GENERATED_GROUP) is collected when BOTH hold:
      • it's older than the grace window (now - first_seen ≥ grace_days), and
      • fewer than GC_PLAYED_FRACTION of its tracks have been played since it was created.

    YouTube exposes no per-playlist play count, so "played" is judged at the song level using each
    track's last-played date: playing the playlist through lights up most of its tracks within the
    window, whereas a couple of stray plays (a song heard via radio/autoplay) doesn't clear the bar.
    Each collection backs up, deletes locally + on YouTube, and records an undoable GC_GENERATED
    action, exactly like a manual delete, just automatic. Returns the collected playlists.
    Failures have durable account/playlist deadlines. Between daily sweeps, retry_only
    checks only saved attempts, while preserving the same age/listening/adoption gates.
    """
    from yt_playlist.rec import rec_params
    if grace_days is None:
        grace_days = rec_params.get_param(store, "generated_gc_days")
    grace_s = grace_days * 86400.0
    clients = clients or {}
    recency = store.charts.get_playlist_track_recency(include_generated=True)
    collected = []
    for pl in _gc_targets(store, recency, now, grace_s):
        client = clients.get(pl.identity_id)
        if client is None:                                # can't delete remotely without its client
            continue
        try:
            with claim_deletion_target(store, pl.identity_id, pl.ytm_playlist_id):
                current = store.playlists.get_playlist_for_account(pl.identity_id, pl.ytm_playlist_id)
                if current is not None:
                    if not _gc_eligible_now(store, current, now, grace_s):
                        playlist_gc_retry.clear(store, pl.identity_id, pl.ytm_playlist_id)
                        continue
                    pl = current
                else:
                    action = (store.actions.get_pending_deletion(pl.identity_id, pl.ytm_playlist_id)
                              or store.actions.get_failed_deletion(pl.identity_id, pl.ytm_playlist_id))
                    if action is None or action.kind != GC_GENERATED:
                        playlist_gc_retry.clear(store, pl.identity_id, pl.ytm_playlist_id)
                        continue
                    entry = next(e for e in json.loads(action.undo_json)["deletions"]
                                 if (e["identity"], e["ytm"]) == (pl.identity_id, pl.ytm_playlist_id))
                    pl = SimpleNamespace(id=None, identity_id=pl.identity_id,
                                         ytm_playlist_id=pl.ytm_playlist_id,
                                         title=entry.get("title", pl.ytm_playlist_id))
                previous = playlist_gc_retry.read(store, pl)
                if (retry_only and previous is None) or (previous and previous["next_retry_at"] > now):
                    continue
                state = playlist_gc_retry.begin(store, pl, now, previous)
                phase = "reconcile"
                try:
                    backup_path = resume_gc_deletion(store, pl, clients, now)
                    if backup_path is None:
                        if pl.id is None:
                            playlist_gc_retry.clear(store, pl.identity_id, pl.ytm_playlist_id)
                            continue
                        phase = "backup"
                        backup_path = backup_playlist(store, pl.id, now, client)
                        # Reconciliation and backup can wait on the network. Listening,
                        # adoption or local removal during that wait must still prevent
                        # a fresh destructive request; retain the durable backup.
                        current = store.playlists.get_playlist_for_account(
                            pl.identity_id, pl.ytm_playlist_id)
                        if current is None or not _gc_eligible_now(store, current, now, grace_s):
                            playlist_gc_retry.clear(store, pl.identity_id, pl.ytm_playlist_id)
                            continue
                        pl = current
                        logger.warning("GC: deleting unplayed generated playlist %s (backup at %s)",
                                       pl.ytm_playlist_id, backup_path)
                        phase = "delete"
                        delete_backed_up(store, pl, client, backup_path, now, kind=GC_GENERATED)
                except WorkClosing:
                    raise
                except Exception as exc:  # one playlist's failure must not stop the sweep
                    playlist_gc_retry.failed(store, pl, state, exc, phase)
                    continue
                playlist_gc_retry.clear(store, pl.identity_id, pl.ytm_playlist_id)
                collected.append({"ytm": pl.ytm_playlist_id, "title": pl.title, "backup": backup_path})
                if previous:
                    logger.info("GC: resolved playlist %s (account %s) after %d attempts",
                                pl.ytm_playlist_id, pl.identity_id, state["attempts"])
        except WorkClosing:
            raise
        except Exception as exc:  # retain the journal if admission or retry storage fails
            logger.warning("GC: could not finish collection of playlist %s (account %s): %s",
                           pl.ytm_playlist_id, pl.identity_id, exc)
    return collected

def create_generated_playlist(store, title, tracks, client, now, identity_id=None, group=None,
                              recipe=None, *, max_duration_s=None) -> dict:
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
    max_duration = rec_params.max_track_duration_s(store) if max_duration_s is None else max_duration_s
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
    recording = not getattr(store, 'activity_paused', False)
    if recording:
        store.playlists.mark_generated_playlist(new_pid)
        store.playlists.set_playlist_group(new_pid, group)
    handles = {}
    addition = account_additions(_add_items, client, new_pid, [t["video_id"] for t in uniq],
                                prior_tracks=[], set_video_ids=handles)
    added, skipped = addition.added, addition.skipped
    db_pid = None
    confirmed = set(addition.confirmed)
    confirmed_tracks = [t for t in uniq if t['video_id'] in confirmed]
    if recording and identity_id is not None:            # optimistic local materialization
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
    if recording:
        store.actions.record_action(COPY_PLAYLIST,
                        json.dumps({"title": title, "source": "recommendations", "group": group,
                                    "added": added, "skipped": len(skipped) + excluded,
                                    "addition": addition.diagnostics(), "added_handles": handles,
                                    "partial": bool(addition.failure)}),
                        "{}", "executed", json.dumps(capture_copy(client, new_pid, identity_id,
                            [t["video_id"] for t in uniq if t["video_id"] not in skipped],
                            title=title, description=GENERATED_DESCRIPTION, addition=addition)), now)
    if recording and recipe is not None:
        store.playlist_recipes.set_recipe(new_pid, recipe, now)           # remember exactly how this one was made
    addition.finish()
    return {"new_ytm": new_pid, "pid": db_pid, "title": title, "added": added,
            "skipped": len(skipped) + excluded, "added_handles": handles,
            "tracks": confirmed_tracks}

