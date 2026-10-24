"""Guided onboarding for thin / new accounts (#62): show a simple seed-your-taste Home view while the
taste model is too thin to drive the normal feed, and graduate automatically once it is ready."""
import random

from yt_playlist.rec import rec_params


def feedback_count(store) -> int:
    """Likes + dislikes recorded so far (the explicit taste signal onboarding collects)."""
    return len(store.rec_model.recent_liked_keys()) + len(store.rec_model.list_dislikes())


def library_size(store) -> int:
    return store.rec_query.tracks_total()


def _setup_complete(*, imported, has_library, mixes_ready) -> bool:
    # Sparse accounts can receive useful prepared offers before modes can be fit.
    # An empty imported account has no library preparation left to wait for.
    return imported and (mixes_ready or not has_library)


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
        complete = _setup_complete(imported=imported, has_library=coverage["total"] > 0,
                                   mixes_ready=mixes_ready)
        phase = "ready" if complete else "import" if not imported else "learn"
        return {"songs": songs, "playlists": len(reader.playlists.get_playlists()),
                "albums": len(reader.collection.get_saved_albums()),
                "covers": list(dict.fromkeys(row["thumbnail"] or
                    f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg" for row in covers)),
                "outcome": reader.settings.get_setting("last_sync_outcome"),
                "setup": {"complete": complete, "phase": phase, "imported": imported,
                          "analyzed": coverage["processed"], "total": coverage["total"],
                          "genres": coverage["genre"], "model_tracks": model_tracks,
                          "modes": modes, "mixes_ready": mixes_ready, "discoveries": discoveries}}


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
    """Keep setup until import and usable offers arrive, independently of modes."""
    return not _setup_complete(
        imported=store.settings.get_setting("last_sync_at") is not None,
        has_library=library_size(store) > 0, mixes_ready=prepared_mixes_ready(store, now))


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
    if store.settings.get_setting("onboard_dismissed") == "1":
        return False
    if not _has_synced(store):
        return False
    if _taste_ready(store) or prepared_mixes_ready(store, now):
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
