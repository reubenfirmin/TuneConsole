"""Guide new accounts until useful listening offers arrive, then retain setup completion."""
import random

from yt_playlist.rec import mix_tuning, rec_params
from yt_playlist.rec.onboarding_outputs import MIN_MIXES, prepared_mixes_ready, starter_album
from yt_playlist.util.duration import parse_duration

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
            if surface == 'choice_window:mixes':
                from yt_playlist.rec import choice_evidence
                offer = choice_evidence.opportunity(store, shown['opportunity_id'])
                if offer is not None and len(offer.get('options', [])) < MIN_MIXES:
                    continue
            return True
    return False


def _remember_completion(store, complete):
    if (complete and not getattr(store, '_read_only', False)
            and not getattr(store, 'activity_paused', False)
            and store.settings.get_setting(SETUP_COMPLETE) != '1'):
        store.settings.set_setting(SETUP_COMPLETE, '1')


def library_size(store) -> int:
    return store.rec_query.tracks_total()


def _setup_complete(*, mixes_ready, albums_ready=False) -> bool:
    # Verified songs can produce useful offers while unrelated imports still
    # need retrying. Full-sync freshness and metadata coverage aren't gates.
    return mixes_ready or albums_ready


def arrival_status(store, now=None) -> dict:
    """Local setup evidence, shared by Home and the app-wide setup banner.

    Metadata coverage is a count, never a percentage pretending to measure model training.
    Observations from incomplete imports are not ready songs.
    """
    from yt_playlist.rec import account_growth, artist_start

    with store.read_view() as reader:
        facts = reader.readiness.arrival()
        songs, covers = facts['songs'], facts['covers']
        coverage = reader.enrichment_reports.coverage_stats()
        modes = facts['modes']
        model_tracks = reader.rec_model.rec_content_vectors_count()
        discoveries = reader.rec_model.rec_discovered_content_vectors_count()
        imported = reader.settings.get_setting("last_sync_at") is not None
        import time
        growth = account_growth.view(reader, time.time() if now is None else now)
        mixes_ready = growth['ready_formats']['mixes']
        albums_ready = growth['ready_formats']['albums']
        artists_required = artist_start.required(reader)
        complete = not artists_required and (setup_completed(reader) or _setup_complete(
            mixes_ready=mixes_ready, albums_ready=albums_ready))
        phase = "artists" if artists_required else "ready" if complete else "import" if not imported else "learn"
        result = {"songs": songs, "playlists": len(reader.playlists.get_playlists()),
                "albums": len(reader.collection.get_saved_albums()),
                "covers": list(dict.fromkeys(row["thumbnail"] or
                    f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg" for row in covers)),
                "outcome": reader.settings.get_setting("last_sync_outcome"),
                "setup": {"complete": complete, "phase": phase, "imported": imported,
                          "artists_required": artists_required,
                          "analyzed": coverage["processed"], "total": coverage["total"],
                          "genres": coverage["genre"], "model_tracks": model_tracks,
                          "modes": modes, "mixes_ready": mixes_ready, "albums_ready": albums_ready,
                          "offers_ready": mixes_ready or albums_ready, "discoveries": discoveries}}
    _remember_completion(store, complete)
    return result


def prepared_albums_ready(store, now=None) -> bool:
    """Album listeners can graduate independently of prepared mixes and fitted modes."""
    import time
    from yt_playlist.rec import growth_readiness
    return growth_readiness.albums(store, time.time() if now is None else now)


def initial_setup_pending(store, now=None) -> bool:
    """Keep setup until useful offers arrive once, independently of modes."""
    if setup_completed(store):
        _remember_completion(store, True)
        return False
    import time
    from yt_playlist.rec import account_growth
    growth = account_growth.view(store, time.time() if now is None else now)
    complete = _setup_complete(mixes_ready=growth['ready_formats']['mixes'],
                               albums_ready=growth['ready_formats']['albums'])
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


def onboarding_active(store, now) -> bool:
    """Keep a synced account in setup until useful offers arrive or it dismisses setup."""
    if setup_completed(store) or store.settings.get_setting("onboard_dismissed") == "1":
        return False
    if not _has_synced(store):
        return False
    return initial_setup_pending(store, now)


def library_sample(store, n=12) -> list[dict]:
    """A genre-diverse sample of owned tracks: round-robin across genre families, so ratings teach
    breadth. Falls back to any owned tracks when genres are sparse."""
    rows = store.readiness.library_sample(n)
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
                        "album": r["album"] or "", "thumbnail": r["thumb"], "key": r["k"],
                        "duration": r["duration"]})
        i += 1
        if not any(fams):
            break
    return out


def _home_tracks(store, client, n):
    """Flatten YouTube home-feed shelves into proto tracks (quick picks / mixes). Best-effort."""
    out = []
    maximum = rec_params.max_track_duration_s(store)
    try:
        shelves = client.get_home() or []
    except Exception:  # noqa: BLE001
        return out
    for shelf in shelves:
        for t in (shelf.get("contents") or []):
            vid = t.get("videoId")
            if not vid:
                continue
            duration = t.get('duration_seconds') or parse_duration(t.get('duration'))
            if duration is None:
                duration = store.tracks.duration_for_video(vid)
            if duration is not None and duration > maximum:
                continue
            arts = t.get("artists") or []
            out.append({"video_id": vid, "title": t.get("title") or "",
                        "artist": (arts[0].get("name") or "" if arts else ""), "album": "",
                        "thumbnail": None, "key": "", "duration": duration})
            if len(out) >= n:
                return out
    return out


def _discovered_sample(store, now, n):
    """Keep candidate selection and its duration eligibility in one local read view."""
    from yt_playlist.rec import surfaces

    mix_tuning.expire(store, now)
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return _discovered_sample(reader, now, n)
    candidates = surfaces.cold_candidates(store, now, limit=n)
    metadata = store.rec_eligibility.offer_metadata([it.video_id for it in candidates])
    maximum = rec_params.max_track_duration_s(store)
    out = []
    for it in candidates:
        duration = metadata.get(it.video_id, {}).get('duration')
        if duration is not None and duration > maximum:
            continue
        out.append({"video_id": it.video_id, "title": it.title, "artist": it.artist,
                    "album": getattr(it, "album", "") or "", "thumbnail": it.thumbnail,
                    "key": it.key, "duration": duration})
    return out


def radio_sample(store, client, now, n=12) -> list[dict]:
    """Onboarding radio: the discovered/cold pool when it has material, topped up from YouTube's home
    feed (get_home) when the account is too empty to have seeded any. [] when there is no client."""
    if client is None:
        return []
    try:
        out = _discovered_sample(store, now, n)
    except Exception:  # noqa: BLE001 - cold pool is best-effort
        out = []
    if len(out) < n:
        seen = {d["video_id"] for d in out}
        for d in _home_tracks(store, client, n - len(out)):
            if d["video_id"] not in seen:
                out.append(d)
    return out[:n]
