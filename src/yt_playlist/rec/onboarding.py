"""Guided onboarding for thin / new accounts (#62): show a simple seed-your-taste Home view while the
taste model is too thin to drive the normal feed, and graduate automatically once it is ready."""
import random

from yt_playlist.rec import rec_params

SETUP_COMPLETE = 'onboard_setup_complete'


def setup_completed(store) -> bool:
    """First-run completion survives disposable offers and model rebuilds.

    Older installations retain actual mix offers or a presented listening menu.
    Use that evidence to graduate them without treating fitted modes, imported
    likes or a dismissed introduction as a usable first offer.
    """
    if store.settings.get_setting(SETUP_COMPLETE) == '1':
        return True
    for surface in ('home_shown', 'choice_window:mixes', 'choice_window:albums'):
        try:
            shown = store.rec_proposals.get_proposals(surface) or {}
        except (ValueError, TypeError):
            continue
        if not isinstance(shown, dict):
            continue
        if surface == 'home_shown':
            signatures = shown.get('signatures')
            if isinstance(signatures, list) and any(isinstance(value, str) and value for value in signatures):
                return True
        elif isinstance(shown.get('opportunity_id'), str) and shown['opportunity_id']:
            return True
    return False


def _remember_completion(store, complete):
    if (complete and not getattr(store, '_read_only', False)
            and not getattr(store, 'activity_paused', False)
            and store.settings.get_setting(SETUP_COMPLETE) != '1'):
        store.settings.set_setting(SETUP_COMPLETE, '1')


def feedback_count(store) -> int:
    """Likes + dislikes recorded so far (the explicit taste signal onboarding collects)."""
    return len(store.rec_model.recent_liked_keys()) + len(store.rec_model.list_dislikes())


def library_size(store) -> int:
    return store.rec_query.tracks_total()


def _setup_complete(*, imported, has_library, mixes_ready, albums_ready=False) -> bool:
    # Sparse accounts can receive useful prepared offers before modes can be fit.
    # An empty imported account has no library preparation left to wait for.
    return imported and (mixes_ready or albums_ready or not has_library)


def arrival_status(store, now=None) -> dict:
    """Local setup evidence, shared by Home and the app-wide setup banner.

    Metadata coverage is a count, never a percentage pretending to measure model training.
    Observations from incomplete imports are not ready songs.
    """
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        songs = reader.conn.execute("SELECT COUNT(*) FROM playable_library_tracks").fetchone()[0]
        covers = reader.conn.execute(
            "SELECT video_id, thumbnail FROM playable_library_tracks ORDER BY identity_key LIMIT 6").fetchall()
        coverage = reader.enrichment_reports.coverage_stats()
        modes = reader.conn.execute("SELECT COUNT(*) FROM taste_modes WHERE active=1").fetchone()[0]
        model_tracks = reader.rec_model.rec_content_vectors_count()
        discoveries = reader.conn.execute("SELECT COUNT(*) FROM rec_discovered_content_vectors").fetchone()[0]
        imported = reader.settings.get_setting("last_sync_at") is not None
        mixes_ready = prepared_mixes_ready(reader, now)
        albums_ready = prepared_albums_ready(reader, now)
        complete = setup_completed(reader) or _setup_complete(
            imported=imported, has_library=coverage["total"] > 0,
            mixes_ready=mixes_ready, albums_ready=albums_ready)
        phase = "ready" if complete else "import" if not imported else "learn"
        result = {"songs": songs, "playlists": len(reader.playlists.get_playlists()),
                "albums": len(reader.collection.get_saved_albums()),
                "covers": list(dict.fromkeys(row["thumbnail"] or
                    f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg" for row in covers)),
                "outcome": reader.settings.get_setting("last_sync_outcome"),
                "setup": {"complete": complete, "phase": phase, "imported": imported,
                          "analyzed": coverage["processed"], "total": coverage["total"],
                          "genres": coverage["genre"], "model_tracks": model_tracks,
                          "modes": modes, "mixes_ready": mixes_ready, "albums_ready": albums_ready,
                          "offers_ready": mixes_ready or albums_ready, "discoveries": discoveries}}
    _remember_completion(store, complete)
    return result


def prepared_albums_ready(store, now=None) -> bool:
    """Album listeners can graduate independently of prepared mixes and fitted modes."""
    import time
    from yt_playlist.rec import album_choices
    payload = album_choices.load(store, time.time() if now is None else now)
    return bool(payload and album_choices.eligible(store, payload['items'], now))


def prepared_mixes_ready(store, now=None) -> bool:
    """Use complete, currently playable offers; fitted modes alone cannot fill Home."""
    import logging
    import time
    from yt_playlist.rec import home_cards, home_prepared

    now = time.time() if now is None else now
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    for surface in ("home_tuned", "home_prepared"):
        try:
            payload = store.rec_proposals.get_proposals(surface)
            if not home_prepared._valid(payload):
                continue
            if surface == "home_tuned" and payload.get("tuning") != home_prepared.tuning_key(store, now):
                continue
            if home_prepared.cards(store, payload, now, service, record=False).protos:
                return True
        except (ValueError, TypeError, KeyError):
            continue
    return False


def initial_setup_pending(store, now=None) -> bool:
    """Keep setup until useful offers arrive once, independently of modes."""
    if setup_completed(store):
        _remember_completion(store, True)
        return False
    complete = _setup_complete(
        imported=store.settings.get_setting("last_sync_at") is not None,
        has_library=library_size(store) > 0, mixes_ready=prepared_mixes_ready(store, now),
        albums_ready=prepared_albums_ready(store, now))
    _remember_completion(store, complete)
    return not complete


def cleanup_count(store) -> int:
    """The CACHED playlist-cleanup count (the worker / cleanup page run the O(n^2) scan and cache it;
    home only ever reads the cached number, never the scan)."""
    from yt_playlist.rec.actions import CLEANUP_SURFACE
    return (store.rec_proposals.get_proposals(CLEANUP_SURFACE) or {}).get("count", 0)


def _has_synced(store) -> bool:
    # A sync of EITHER kind has run (matches actions.sync_status). NOT enrichment-processed: an empty
    # account has nothing to enrich, but is still synced and must see onboarding (radio).
    return any(store.settings.get_setting(k) is not None for k in ("last_sync_at", "last_plays_sync_at"))


def _taste_ready(store) -> bool:
    # At least one active taste mode means the model can drive the normal feed. A mode cannot be built
    # without enriched content vectors, so its existence already implies enrichment ran - we do not
    # also gate on a coverage percentage (the `processed` bookkeeping is an unreliable proxy, and
    # coverage without modes can't fill the mode cards anyway).
    return bool(store.modes.list_modes(active_only=True))


def onboarding_active(store, now) -> bool:
    """True while a synced account is still thin and has not graduated (ready / enough feedback /
    dismissed). The inverse of the graduation condition."""
    if setup_completed(store) or store.settings.get_setting("onboard_dismissed") == "1":
        return False
    if not _has_synced(store):
        return False
    if _taste_ready(store) or prepared_mixes_ready(store, now) or prepared_albums_ready(store, now):
        return False
    if feedback_count(store) >= rec_params.get_param(store, "onboard_feedback_min"):
        return False
    return True


def library_sample(store, n=12) -> list[dict]:
    """A genre-diverse sample of owned tracks: round-robin across genre families, so ratings teach
    breadth. Falls back to any owned tracks when genres are sparse."""
    with store.read_snapshot() as snapshot:
        rows = snapshot.store.conn.execute(
            "SELECT identity_key k, title, artist, album, video_id vid, thumbnail thumb, genre "
            "FROM playable_library_tracks").fetchall()
    if not rows:
        return []
    by_fam = {}
    rng = random.Random()       # fresh draw each visit, so the user doesn't see the same 12 tracks for days
    for r in rows:
        by_fam.setdefault(r["genre"] or "", []).append(r)
    for fam in by_fam.values():
        rng.shuffle(fam)
    out, fams = [], [f for f in by_fam.values()]
    rng.shuffle(fams)
    i = 0
    while len(out) < n and any(fams):
        f = fams[i % len(fams)]
        if f:
            r = f.pop()
            out.append({"video_id": r["vid"], "title": r["title"], "artist": r["artist"],
                        "album": r["album"] or "", "thumbnail": r["thumb"], "key": r["k"]})
        i += 1
        if not any(fams):
            break
    return out


def starter_album(store):
    """Choose a real release immediately, without models or provider requests.

    Saved albums need no materialized tracks: the existing album player obtains
    the complete release at play time. Otherwise prefer known releases by artists
    with several owned songs or listening days. An incidental track is never
    presented as a complete album tracklist.
    """
    from yt_playlist.library.album_fetch import is_upload_album

    def usable(album):
        browse = album.get('browse') or ''
        return (browse.startswith('MPRE') or is_upload_album(browse)) and bool(album.get('title'))

    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        saved = [album for album in reader.collection.get_saved_albums() if usable(album)]
        if saved:
            album = next((album for album in saved if album.get('type') != 'Single'), saved[0])
            return {**album, 'saved': True, 'upload': is_upload_album(album['browse'])}
        # Count distinct owned recordings, not playlist occurrences or partial
        # imports. Catalog releases need not have been saved to be worth trying.
        rows = reader.conn.execute(
            "WITH familiar AS (SELECT t.artist,COUNT(DISTINCT t.identity_key) songs,"
            "SUM(COALESCE(d.plays,0)) plays FROM playable_library_tracks t "
            "LEFT JOIN listening_day_counts d ON d.identity_key=t.identity_key GROUP BY t.artist) "
            "SELECT p.provider_id browse,a.title,a.artist,a.year,a.type,a.thumbnail,f.songs,f.plays "
            "FROM albums a JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "JOIN familiar f ON f.artist=a.artist COLLATE NOCASE "
            "ORDER BY (f.songs>=2 OR f.plays>=2) DESC,f.plays DESC,f.songs DESC,a.title COLLATE NOCASE"
        ).fetchall()
        album = next((dict(row) for row in rows if usable(dict(row))), None)
    return {**album, 'saved': False, 'upload': is_upload_album(album['browse'])} if album else None


def _home_tracks(client, n):
    """Flatten YouTube home-feed shelves into proto tracks (quick picks / mixes). Best-effort."""
    out = []
    try:
        shelves = client.get_home() or []
    except Exception:  # noqa: BLE001
        return out
    for shelf in shelves:
        for t in (shelf.get("contents") or []):
            vid = t.get("videoId")
            if not vid:
                continue
            arts = t.get("artists") or []
            out.append({"video_id": vid, "title": t.get("title") or "",
                        "artist": (arts[0].get("name") or "" if arts else ""), "album": "",
                        "thumbnail": None, "key": ""})
            if len(out) >= n:
                return out
    return out


def radio_sample(store, client, now, n=12) -> list[dict]:
    """Onboarding radio: the discovered/cold pool when it has material, topped up from YouTube's home
    feed (get_home) when the account is too empty to have seeded any. [] when there is no client."""
    if client is None:
        return []
    from yt_playlist.rec import surfaces
    out = []
    try:
        for it in surfaces.cold_candidates(store, now, limit=n):
            out.append({"video_id": it.video_id, "title": it.title, "artist": it.artist,
                        "album": getattr(it, "album", "") or "", "thumbnail": it.thumbnail,
                        "key": it.key})
    except Exception:  # noqa: BLE001 - cold pool is best-effort
        out = []
    if len(out) < n:
        seen = {d["video_id"] for d in out}
        for d in _home_tracks(client, n - len(out)):
            if d["video_id"] not in seen:
                out.append(d)
    return out[:n]
