import logging
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.activity import WorkClosing, check_stopping
from yt_playlist.util.matching import identity_key, track_artist, track_album
from yt_playlist.util.retry import error_summary, with_retry
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.library.playlist_metadata import content_hash, is_generated_description
from yt_playlist.library.playlist_snapshot import is_complete_playlist, validate_playlist
from yt_playlist.library import ratings
from yt_playlist.library.playlist_positions import retry_insertions
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.library.sync_outcomes import (begin_attempt, combined_status, finish_attempt,
                                               resource, retained, SyncSchedule)
from yt_playlist.repos.base import GENERATED_GROUP

logger = logging.getLogger(__name__)

_artist = track_artist
_album = track_album

def _artist_id(t):
    arts = t.get("artists") or []
    return arts[0].get("id") if arts and isinstance(arts[0], dict) else None

def _album_id(t):
    alb = t.get("album")
    return alb.get("id") if isinstance(alb, dict) else None

# Within one sync run, a track's rated-map status is the HIGHEST-priority status seen across every
# playlist it appears in. YTM's likeStatus is per-context: a liked track that also sits in ordinary
# playlists reads INDIFFERENT there (the rating simply is not populated in that context), so
# last-writer-wins would flap the same track LIKE/INDIFFERENT run to run depending on fetch order,
# deleting and re-recording the like each day. LIKE must beat INDIFFERENT; only DISLIKE beats LIKE.
# Only a COMPLETE run of INDIFFERENT observations can establish a genuine un-like.
_STATUS_PRIORITY = {"INDIFFERENT": 0, "LIKE": 1, "DISLIKE": 2}


def _complete_rating_context(detail):
    """Missing pages or unpopulated ratings leave this playlist's evidence unknown."""
    return (is_complete_playlist(detail)
            and all(t.get("likeStatus") in _STATUS_PRIORITY for t in detail["tracks"]))


def _sync_generated_group(store, ytm, description, groups, identity_id=None):
    """Reconcile explicit remote policy while preserving the user's display group."""
    ytm = store.playlists.playlist_reference(ytm,identity_id)
    store.playlists.reconcile_playlist_provenance(ytm, description)
    marked = is_generated_description(description)
    current = groups.get(ytm)
    if marked and current is None:
        store.playlists.set_playlist_group(ytm, GENERATED_GROUP)
    elif (not store.playlists.is_quarantined_playlist(ytm)) and current == GENERATED_GROUP:
        store.playlists.set_playlist_group(ytm, "")


def _emit(on_progress, type, text, **extra):
    """Report a progress event to an optional callback (used to stream sync to the browser)."""
    check_stopping()
    if on_progress is not None:
        on_progress({"type": type, "text": text, **extra})

def _is_auth_error(e) -> bool:
    from yt_playlist.core.album_snapshot import LibrarySnapshotError
    if isinstance(e, LibrarySnapshotError):
        return e.reason == "signed out"
    # A dead session shows up two ways: a clean HTTP 401/403, or (more often) YouTube quietly serving
    # the SIGNED-OUT layout - singleColumnBrowseResultsRenderer with a "Sign in" button / signInEndpoint -
    # which ytmusicapi can't parse (it expects the signed-in twoColumnBrowseResultsRenderer) and raises a
    # nav error whose text carries those markers. Treat both as expired so the user gets the re-auth banner
    # instead of a silent "unavailable (skipped)".
    s = str(e).lower()
    return ("401" in s or "403" in s or "unauthorized" in s
            or "signinendpoint" in s or "singlecolumnbrowseresultsrenderer" in s)

def sync_identity(store, identity_id, client, now, on_progress=None, label=None,
                  on_auth_expired=None, on_auth_ok=None, *, schedule=None):
    schedule = schedule or SyncSchedule(store, now)
    label = label or f"identity {identity_id}"
    try:
        resources, auth_bad = _sync_playlists(store, identity_id, client, now, on_progress,
                                             label, on_auth_expired, schedule)
    except WorkClosing:
        raise
    except Exception as exc:
        detail = error_summary(exc)
        resources = {name: resource("failed", detail)
                     for name in ("playlist_listing", "playlists", "ratings")}
        resources["playlists"]["items"] = {pid: retained(item) for pid, item in
            schedule.previous(identity_id, "playlists").get("items", {}).items()}
        auth_bad = _is_auth_error(exc)
        logger.warning("playlist-library fetch failed for identity %s: %s", identity_id, detail)
        _emit(on_progress, "err", f"{label}: playlist library unavailable: {detail}")
    if schedule.resource_due(identity_id, "history"):
        resources["history"], history_auth_bad = _sync_history(
            store, identity_id, client, now, on_progress, label, on_auth_expired)
        auth_bad = auth_bad or history_auth_bad
    else:
        resources["history"] = retained(schedule.previous(identity_id, "history"))
    if not auth_bad:
        ratings.reconcile_pending(store, identity_id, client)
        if on_auth_ok and any(r.get("status") == "complete" and not r.get("_retained")
                              for r in resources.values()):
            on_auth_ok(identity_id)
    status = combined_status(resources.values())
    playlists = resources["playlists"]
    _emit(on_progress, "info", f"{label}: {status} ({playlists.get('refreshed', 0)} playlists refreshed, "
          f"{playlists.get('skipped', 0)} skipped)")
    return {"label": label, "resources": resources, "status": status}


def _sync_playlists(store, identity_id, client, now, on_progress, label, on_auth_expired, schedule):
    label = label or f"identity {identity_id}"
    resources = {}
    auth_bad = False

    def result():
        return resources, auth_bad

    def unavailable(status, message):
        resources.update({name: resource(status, message)
                          for name in ("playlist_listing", "playlists", "ratings")})
        resources["playlists"]["items"] = {pid: retained(item) for pid, item in
            schedule.previous(identity_id, "playlists").get("items", {}).items()}
        return result()
    if (not schedule.resource_due(identity_id, "playlist_listing") and
            schedule.previous(identity_id, "playlist_listing").get("status") != "complete"):
        return {name: retained(schedule.previous(identity_id, name))
                for name in ("playlist_listing", "playlists", "ratings")}, False
    logger.info("syncing identity %s", identity_id)
    # Only these rows can be disproved by this library snapshot. A playlist created while
    # the remote fetch is in flight may not appear in it yet, even while it is playing.
    existing_playlists = [p for p in store.playlists.get_playlists() if p.identity_id == identity_id]
    _emit(on_progress, "info", f"{label}: fetching playlists…")
    try:
        if schedule.resource_due(identity_id, "playlist_listing"):
            playlists = with_retry(lambda: client.get_library_playlists(limit=None),
                                   operation=f"fetch library for {label!r} (identity {identity_id})",
                                   check_cancelled=check_stopping)
            resources["playlist_listing"] = resource("complete", entries=playlists)
            resources["playlist_listing"]["verified_empty"] = (not playlists and
                                                                  getattr(playlists, "complete", False))
        else:
            resources["playlist_listing"] = retained(schedule.previous(identity_id, "playlist_listing"))
            playlists = resources["playlist_listing"].get("entries", [])
        if (not isinstance(playlists, list)
                or any(not isinstance(p, dict) or not p.get("playlistId") for p in playlists)):
            raise ValueError("playlist snapshot is unavailable")
    except WorkClosing:
        raise
    except BridgeError as e:
        # The extension is disconnected / no signed-in tab / a bridge timeout: a CONNECTION problem,
        # not a dead session. Skip this identity quietly, do NOT flag re-auth, do NOT raise. The
        # background sync daemon retries once the extension reconnects.
        logger.info("identity %s: extension not connected, skipping (%s)", identity_id, e)
        _emit(on_progress, "info", f"{label}: extension not connected, skipping")
        return unavailable("skipped", "Extension not connected")
    except Exception as e:  # noqa: BLE001 - an expired session shouldn't abort the whole sync
        if _is_auth_error(e):
            auth_bad = True
            logger.warning("auth expired for identity %s: %s", identity_id, e)
            if on_auth_expired:
                on_auth_expired(identity_id, label)
            _emit(on_progress, "auth_expired",
                  f"{label}: YouTube session expired. Re-authenticate", label=label)
            return unavailable("failed", "YouTube session expired")
        raise
    _emit(on_progress, "info", f"{label}: {len(playlists)} playlists", count=len(playlists))
    total = len(playlists)
    seen_ytm = set()
    groups = store.playlists.get_playlist_groups()        # for the generated-quarantine reconciliation below
    rated: dict[str, str] = {}                  # videoId -> likeStatus; see _STATUS_PRIORITY
    ratings_complete = True
    refreshed = skipped = incomplete = reused = 0
    previous_items = schedule.previous(identity_id, "playlists").get("items", {})
    previous_listing = schedule.previous(identity_id, "playlist_listing")
    # Periodic full reads also reconcile neutral ratings. Retries between those
    # sweeps reuse healthy membership without claiming fresh neutral evidence.
    full_refresh = (previous_listing.get("status") == "complete" and
                    schedule.resource_due(identity_id, "playlist_listing"))
    items = {}
    for i, pl in enumerate(playlists, 1):
        check_stopping()
        pid = pl["playlistId"]
        context = f"playlist {pl.get('title', '')!r} ({pid}), account {label!r} (identity {identity_id})"
        seen_ytm.add(pid)  # mark seen before fetch so a transient read failure doesn't prune it
        previous = previous_items.get(pid, {})
        if not full_refresh and not schedule.due(previous):
            items[pid] = retained(previous)
            reused += 1
            # An older snapshot cannot prove that a rating became neutral today.
            ratings_complete = False
            continue
        try:
            detail = with_retry(lambda: client.get_playlist(pid, limit=None), operation=f"fetch {context}",
                                check_cancelled=check_stopping)
            if (not isinstance(detail, dict) or not isinstance(detail.get("tracks"), list)
                    or any(not isinstance(t, dict) for t in detail["tracks"])
                    or detail.get("error")
                    or ("status" in detail and detail["status"] != "STATUS_SUCCEEDED")):
                raise ValueError("playlist tracks unavailable")
        except WorkClosing:
            raise
        except Exception as e:  # noqa: BLE001 - one bad/just-deleted playlist must not abort the sync
            skipped += 1
            ratings_complete = False
            if _is_auth_error(e):
                auth_bad = True
                if on_auth_expired:
                    on_auth_expired(identity_id, label)
            logger.warning("skipping %s: %s; keeping any cached playlist/tracks. "
                           "Check https://music.youtube.com/playlist?list=%s with this account; "
                           "full exception at DEBUG", context, error_summary(e), pid)
            _emit(on_progress, "info", f"{label} › skipped {pl.get('title', '')} (couldn't read)")
            items[pid] = resource("failed", error_summary(e))
            continue
        ratings_complete = ratings_complete and _complete_rating_context(detail)
        complete = is_complete_playlist(detail)
        if not complete:
            incomplete += 1
            count = detail.get("trackCount")
            advertised = count if type(count) is int else "missing" if count is None else "invalid"
            reason = (f"advertised={advertised}, received={len(detail['tracks'])}, "
                      f"continuation={bool(detail.get('continuation') or detail.get('continuations'))}")
            audit = detail.get('_readAudit')
            if isinstance(audit, dict):
                reason += ', ' + ', '.join(f'{field}={audit.get(field)}' for field in
                    ('pages', 'raw_items', 'omitted', 'unidentified', 'exhausted'))
            items[pid] = resource("partial", reason)
        track_ids, keys = [], []
        for t in detail.get("tracks", []):
            if complete:
                tid = store.tracks.upsert_track(
                    t.get("videoId"), t.get("title", ""), _artist(t),
                    _album(t), t.get("duration_seconds"), t.get("isAvailable"),
                    t.get("videoType"), _artist_id(t), _album_id(t), best_thumb(t.get("thumbnails")),
                    credits=t.get('artists'))
                track_ids.append(tid)
            else:
                # Retain positive catalog/rating evidence without turning a page
                # into library membership, including for a newly discovered list.
                store.tracks.observe_track_entry(t.get("videoId"), t.get("title", ""), _artist(t),
                                                 now=now, album=_album(t), album_browse_id=_album_id(t),
                                                 credits=t.get('artists'))
            status = t.get("likeStatus")
            if status in _STATUS_PRIORITY and t.get("videoId"):
                rk = t["videoId"]
                if rk not in rated or _STATUS_PRIORITY[status] > _STATUS_PRIORITY[rated[rk]]:
                    rated[rk] = status
        if not complete:
            _sync_generated_group(store, pid, detail.get("description"), groups, identity_id)
            logger.warning("incomplete %s (%s); keeping any cached playlist/tracks until a complete read",
                           context, reason)
            _emit(on_progress, "info", f"{label} › {pl.get('title', '')} (incomplete; waiting for a complete read)",
                  i=i, total=total)
            continue
        track_ids = list(dict.fromkeys(track_ids))   # de-dupe (YouTube can repeat a video; see set_playlist_tracks)
        keys = [store.tracks.catalog_subject(tid)["identity_key"] for tid in track_ids]
        chash = content_hash(keys)
        db_pid = store.playlists.upsert_playlist(identity_id, pid, pl.get("title", ""),
                                       len(track_ids), chash, now,
                                       best_thumb(pl.get("thumbnails")) or best_thumb(detail.get("thumbnails")))
        store.playlists.set_playlist_tracks(db_pid, track_ids)
        retry_insertions(store, db_pid, client, now, attempts=1)
        store.playlists.ensure_playlist_thumbnail(db_pid)
        _sync_generated_group(store, pid, detail.get("description"), groups,identity_id)
        refreshed += 1
        items[pid] = resource("complete")
        _emit(on_progress, "step", f"{label} › {pl.get('title', '')} ({len(track_ids)} tracks)",
              i=i, total=total)

    # Prune playlists no longer in this identity's remote library (deleted elsewhere / stale rows),
    # but ONLY when the fetch actually returned a library. An empty result is almost always a
    # transient or session glitch (it doesn't always surface as a 401/403), and pruning on it would
    # destructively wipe every playlist for the identity. Sync must only *update* on success, never
    # clear on a non-result. A later, real sync re-adds anything genuinely missing.
    verified_empty = not playlists and resources["playlist_listing"].get("verified_empty", False)
    if (playlists or verified_empty) and not resources["playlist_listing"].get("_retained"):
        stale = [p for p in existing_playlists if p.ytm_playlist_id not in seen_ytm]
        for p in stale:
            store.playlists.remove_playlist(p.id)
        if stale:
            _emit(on_progress, "info", f"{label}: removed {len(stale)} playlist(s) no longer present")
    elif not playlists and not verified_empty:
        # An empty library on a configured identity is, in practice, a broken/expired session. Some
        # endpoints return [] instead of raising a 401 (exactly what happened to the master account
        # while the brand account got a clean 401). Treat it the SAME: flag for re-auth, never prune.
        auth_bad = True
        logger.warning("identity %s returned no playlists, flagging for re-auth, keeping existing",
                       identity_id)
        if on_auth_expired:
            on_auth_expired(identity_id, label)
        _emit(on_progress, "auth_expired",
              f"{label}: returned no playlists. Session may have expired, re-authenticate", label=label)
        resources["playlist_listing"] = resource("failed", "Unverified empty playlist snapshot")

    incomplete_summary = f", {incomplete} incomplete" if incomplete else ""
    playlist_summary = f"{refreshed} refreshed, {skipped} skipped{incomplete_summary}, {reused} reused"
    item_status = combined_status(items.values()) if items else "complete"
    resources["playlists"] = resource(
        "failed" if auth_bad else item_status,
        "No playlist snapshot" if auth_bad else playlist_summary,
        refreshed=refreshed, skipped=skipped, incomplete=incomplete, reused=reused, items=items)
    if reused == len(playlists) and resources["playlist_listing"].get("_retained"):
        resources["playlists"]["_retained"] = True
    rating_status = ("complete" if ratings_complete and not auth_bad else
                     "partial" if any(s != "INDIFFERENT" for s in rated.values()) else "skipped")
    resources["ratings"] = resource(rating_status,
                                    "" if ratings_complete and not auth_bad else
                                    "Incomplete playlist evidence; neutral ratings preserved")
    if reused and schedule.previous(identity_id, "ratings"):
        resources["ratings"] = retained(schedule.previous(identity_id, "ratings"))

    # An unread context might contain the LIKE/DISLIKE hidden by an ordinary playlist's
    # INDIFFERENT. Apply the same trustworthy evidence to account ratings and model feedback.
    if not ratings_complete:
        rated = {video_id: status for video_id, status in rated.items() if status != "INDIFFERENT"}
    ratings.record_observations(store, identity_id, rated, source="sync", now=now)
    logger.info("synced identity %s (%s): %d playlists listed, %d refreshed, %d skipped, %d incomplete, %d reused",
                identity_id, label, len(playlists), refreshed, skipped, incomplete, reused)
    return result()


def _sync_history(store, identity_id, client, now, on_progress, label, on_auth_expired):
    _emit(on_progress, "info", f"{label}: fetching history…")
    try:  # history is best-effort (powers stale detection); never let it fail the whole sync
        history = with_retry(lambda: client.get_history(),
                             operation=f"fetch history for {label!r} (identity {identity_id})",
                             check_cancelled=check_stopping)
        if not isinstance(history, list) or any(not isinstance(t, dict) for t in history):
            raise ValueError("history snapshot is unavailable")
        hist = [(store.tracks.observe_track(t["videoId"], t.get("title", ""), _artist(t), now=now)
                 if t.get("videoId") else
                 (store.tracks.resolve_identity_key(identity_key(t.get("title", ""), _artist(t)))
                  or identity_key(t.get("title", ""), _artist(t))), t.get("played")) for t in history]
        # Record plays keyed by (track, played-date): YouTube re-sends the same recently-played window
        # each fetch (~91% overlap), so record_history_plays dedups on the absolute play-date (the
        # relative `played` bucket resolved via `now`). #75: this daily fetch is now BACKFILL for plays
        # that happened while the app was not running; live plays arrive via the extension's play
        # events (library/live_plays.py) and dedup against the same (track, date) model.
        store.history.record_history_plays(identity_id, now, hist,
                                   source_timezone=getattr(client, "history_timezone", None))
        return resource("complete", refreshed=len(hist)), False
    except WorkClosing:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("history fetch failed for identity %s: %s", identity_id, error_summary(e))
        _emit(on_progress, "info", f"{label}: history unavailable (skipped)")
        auth_bad = _is_auth_error(e)
        if auth_bad:
            if on_auth_expired:
                on_auth_expired(identity_id, label)
        return resource("failed", error_summary(e)), auth_bad

def refresh_playlist(store, identity_id, client, ytm_playlist_id, title, now, *, snapshot=None) -> None:
    """Re-fetch a single playlist's tracks into the store, fast post-merge refresh (no full sync)."""
    if snapshot is None:
        detail = with_retry(lambda: client.get_playlist(ytm_playlist_id, limit=None))
        if not is_complete_playlist(detail):
            raise ValueError("YouTube did not return a complete playlist; cached membership preserved")
    else:
        detail = validate_playlist(snapshot)
    track_ids, keys = [], []
    for t in detail.get("tracks", []):
        tid = store.tracks.upsert_track(t.get("videoId"), t.get("title", ""), _artist(t),
                                 _album(t), t.get("duration_seconds"), t.get("isAvailable"),
                                 t.get("videoType"), _artist_id(t), _album_id(t), best_thumb(t.get("thumbnails")),
                                 credits=t.get('artists'))
        track_ids.append(tid)
    track_ids = list(dict.fromkeys(track_ids))   # de-dupe repeated videos
    keys = [store.tracks.catalog_subject(tid)["identity_key"] for tid in track_ids]
    db_pid = store.playlists.upsert_playlist(identity_id, ytm_playlist_id, title,
                                   len(track_ids), content_hash(keys), now, best_thumb(detail.get("thumbnails")))
    store.playlists.set_playlist_tracks(db_pid, track_ids)
    retry_insertions(store, db_pid, client, now, attempts=1)
    store.playlists.ensure_playlist_thumbnail(db_pid)
    _sync_generated_group(store, ytm_playlist_id, detail.get('description'),
                          store.playlists.get_playlist_groups(),identity_id)

def _sync_saved_albums(store, clients, on_progress, *, schedule=None):
    """Refresh each complete account snapshot without deleting another account's evidence."""
    saved, snapshots, revisions = {}, {}, {}
    previous_saved_ids = store.collection.saved_album_ids() if schedule else set()
    configured = {identity.id for identity in store.identities.get_identities()}
    complete = bool(clients) and configured <= clients.keys()
    outcomes = {str(i): (retained(schedule.previous(i, "saved_albums"))
                           if schedule and not schedule.resource_due(i, "saved_albums") else
                           resource("skipped", "Account unavailable"))
                for i in configured - clients.keys()}
    for identity_id, client in clients.items():
        check_stopping()
        if schedule and not schedule.resource_due(identity_id, "saved_albums"):
            outcomes[str(identity_id)] = retained(schedule.previous(identity_id, "saved_albums"))
            complete = complete and outcomes[str(identity_id)]["status"] == "complete"
            continue
        try:
            revisions[identity_id] = store.collection.saved_album_revision(identity_id)
            albums = with_retry(lambda: client.get_library_albums(limit=None), check_cancelled=check_stopping)
            if not isinstance(albums, list):
                raise ValueError("saved-album snapshot is unavailable")
            account = []
            for a in albums:
                bid = a.get("browseId")
                if not isinstance(bid, str) or not bid.strip():
                    raise ValueError("saved-album snapshot contains an unidentified album")
                account.append({"browse": bid, "title": a.get("title"),
                              "artist": ", ".join(x.get("name", "") for x in (a.get("artists") or [])),
                              "credits": a.get('artists'),
                              "year": a.get("year"), "type": a.get("type"),
                              "thumbnail": best_thumb(a.get("thumbnails"))})
            snapshots[identity_id] = account
            outcomes[str(identity_id)] = resource("complete", refreshed=len(albums))
        except WorkClosing:
            raise
        except Exception as e:  # noqa: BLE001
            complete = False
            logger.warning("saved-albums fetch failed: %s", e)
            outcomes[str(identity_id)] = resource("skipped" if isinstance(e, BridgeError) else "failed",
                                                  error_summary(e))
    if not snapshots:
        if schedule:
            tracks = (_materialize_album_tracks(store, clients,
                       {a['browse']: a for a in store.collection.get_saved_albums()}, on_progress)
                      if schedule.resource_due(None, "album_tracks") else
                      retained(schedule.previous(None, "album_tracks")))
            mirror = schedule.previous(None, "saved_album_mirror")
            return outcomes, (retained(mirror) if mirror else
                              resource("skipped", "Complete account snapshots unavailable")), tracks
        _emit(on_progress, "info", "saved albums unchanged: no complete account snapshots")
        return outcomes, resource("skipped", "Complete account snapshots unavailable"), resource(
            "skipped", "Saved-album mirror unchanged")
    check_stopping()
    accepted = store.collection.replace_saved_album_snapshots(snapshots, expected_revisions=revisions)
    for identity_id in snapshots.keys()-accepted:
        complete = False
        outcomes[str(identity_id)] = resource('skipped', 'Account or saved library changed during fetch')
    if not accepted:
        _emit(on_progress, 'info', 'saved albums unchanged: account snapshots became stale')
        return outcomes, resource('skipped', 'Account snapshots became stale'), resource(
            'skipped', 'Saved-album mirror unchanged')
    saved = {a['browse']:a for a in store.collection.get_saved_albums()}
    _emit(on_progress, "info", f"saved albums: {len(saved)}")
    tracks = (_materialize_album_tracks(store, clients, saved, on_progress)
              if schedule is None or schedule.resource_due(None, "album_tracks") or
              bool(saved.keys() - previous_saved_ids)
              else retained(schedule.previous(None, "album_tracks")))
    return outcomes, resource("complete" if complete else "partial", refreshed=len(saved)), tracks


def _materialize_album_tracks(store, clients, saved, on_progress):
    """Fold each saved album's TRACKS into the library so they count in the taste corpus (the model
    is built from your tracks; metadata alone doesn't). Incremental: a cached full track list
    proves materialization. An incidental playlist track cannot stand in for a complete album."""
    from yt_playlist.core.accounts import catalog_client
    client = catalog_client(clients)
    if client is None:
        return resource("skipped", "Account unavailable")
    cached = store.collection.complete_album_tracklist_ids()
    todo = [bid for bid in saved if bid not in cached]
    if not todo:
        return resource("complete", refreshed=0)
    # Each album is a separate get_album network call, so stream one step per album (named, with a
    # running counter) instead of going silent for the whole batch. 258 of these is a long wait.
    _emit(on_progress, "info", f"folding in {len(todo)} saved album(s)…", count=len(todo))
    added = failed = 0
    for i, bid in enumerate(todo, 1):
        meta = saved[bid]
        _emit(on_progress, "step",
              f"albums › {i}/{len(todo)} {meta.get('title') or '(album)'}, {meta.get('artist') or '?'}",
              count=len(todo), index=i)
        try:
            album = with_retry(lambda: client.get_album(bid), check_cancelled=check_stopping)
            if not isinstance(album, dict) or not isinstance(album.get("tracks"), list):
                raise ValueError("album track list unavailable")
        except WorkClosing:
            raise
        except Exception as e:  # noqa: BLE001 - one album's failure shouldn't abort the rest
            logger.warning("album-tracks fetch failed for %s: %s", bid, e)
            failed += 1
            continue
        for t in (album or {}).get("tracks") or []:
            if not t.get("title"):
                continue
            artist = ", ".join(x.get("name", "") for x in (t.get("artists") or [])) or meta["artist"]
            store.tracks.upsert_track(t.get("videoId"), t.get("title"), artist, meta["title"], None,
                               album_browse_id=bid, thumbnail=meta["thumbnail"],credits=t.get('artists') or album.get('artists'))
            added += 1
        remember_album(store, bid, album, meta)
        if bid not in store.collection.complete_album_tracklist_ids():
            failed += 1
    _emit(on_progress, "info", f"album tracks folded in: {added} from {len(todo)} album(s)")
    if failed:
        return resource("partial" if failed < len(todo) else "failed",
                        f"{failed} albums unavailable", refreshed=added, skipped=failed)
    return resource("complete", refreshed=added)

def sync_all(store, clients, now, on_progress=None, on_auth_expired=None, on_auth_ok=None, *,
             automatic=False, retry_now=False, max_age_s=86400):
    schedule = SyncSchedule(store, now, automatic=automatic, retry_now=retry_now, max_age_s=max_age_s)
    begin_attempt(store, now)
    labels = {idn.id: idn.label for idn in store.identities.get_identities()}
    accounts = {str(i): {"label": label, "status": "skipped", "resources": {
        name: (resource("skipped", "Account unavailable") if schedule.resource_due(i, name) else
               retained(schedule.previous(i, name)))
        for name in ("playlist_listing", "playlists", "ratings", "history")}}
        for i, label in labels.items() if i not in clients}
    for identity_id, client in clients.items():
        check_stopping()
        try:
            accounts[str(identity_id)] = sync_identity(
                store, identity_id, client, now, on_progress=on_progress, label=labels.get(identity_id),
                on_auth_expired=on_auth_expired, on_auth_ok=on_auth_ok, schedule=schedule)
        except WorkClosing:
            raise
        except Exception as e:  # keep healthy accounts usable when another account fails
            detail = error_summary(e)
            _emit(on_progress, "err", f"{labels.get(identity_id, identity_id)}: sync failed: {detail}")
            accounts[str(identity_id)] = {"label": labels.get(identity_id), "status": "failed",
                "resources": {name: resource("failed", detail)
                              for name in ("playlist_listing", "playlists", "ratings", "history")}}
    albums, mirror, tracks = _sync_saved_albums(store, clients, on_progress, schedule=schedule)
    for identity_id, account in accounts.items():
        account["resources"]["saved_albums"] = albums[identity_id]
        account["status"] = combined_status(account["resources"].values())
        if all(item.get("_retained") for item in account["resources"].values()):
            account["_retained"] = True
    resources = {"saved_album_mirror": mirror, "album_tracks": tracks}
    status = combined_status([*accounts.values(), *resources.values()])
    result = finish_attempt(store, {"status": status, "accounts": accounts, "resources": resources}, now)
    # the recommendation model is rebuilt by the decoupled RecWorker (triggered from the sync
    # route), so a burst of syncs coalesces into one rebuild instead of blocking each sync.
    _emit(on_progress, "done" if status == "complete" else "info", f"sync {status}",
          final=True, outcome=status)
    return result
