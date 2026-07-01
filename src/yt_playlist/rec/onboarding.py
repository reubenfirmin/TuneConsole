"""Guided onboarding for thin / new accounts (#62): show a simple seed-your-taste Home view while the
taste model is too thin to drive the normal feed, and graduate automatically once it is ready."""
import random

from yt_playlist.rec import rec_params
from yt_playlist.rec.rec_dao import RecDao


def _genre_coverage(store) -> float:
    cov = store.coverage_stats()
    processed = cov.get("processed", 0)
    return (cov.get("genre", 0) / processed) if processed else 0.0


def warmup_progress(store) -> int:
    """A 0-100 'warming up' indicator for the onboarding explainer: how far enrichment coverage has
    come toward the readiness target (onboard_coverage_min). Rough and capped at 100 - not the literal
    graduation gate (that is modes-exist), just an honest sense of progress as enrichment fills in."""
    target = rec_params.get_param(store, "onboard_coverage_min") or 1.0
    return int(round(min(1.0, _genre_coverage(store) / target) * 100))


def feedback_count(store) -> int:
    """Likes + dislikes recorded so far (the explicit taste signal onboarding collects)."""
    return len(store.recent_liked_keys()) + len(store.list_dislikes())


def library_size(store) -> int:
    return RecDao(store).tracks_total()


def cleanup_count(store) -> int:
    """The CACHED playlist-cleanup count (the worker / cleanup page run the O(n^2) scan and cache it;
    home only ever reads the cached number, never the scan)."""
    from yt_playlist.rec.actions import CLEANUP_SURFACE
    return (store.get_proposals(CLEANUP_SURFACE) or {}).get("count", 0)


def _has_synced(store) -> bool:
    # A sync of EITHER kind has run (matches recommend.sync_status). NOT enrichment-processed: an empty
    # account has nothing to enrich, but is still synced and must see onboarding (radio).
    return any(store.get_setting(k) is not None for k in ("last_sync_at", "last_plays_sync_at"))


def _taste_ready(store) -> bool:
    # At least one active taste mode means the model can drive the normal feed. A mode cannot be built
    # without enriched content vectors, so its existence already implies enrichment ran - we do not
    # also gate on a coverage percentage (the `processed` bookkeeping is an unreliable proxy, and
    # coverage without modes can't fill the mode cards anyway).
    return bool(store.modes.list_modes(active_only=True))


def onboarding_active(store, now) -> bool:
    """True while a synced account is still thin and has not graduated (ready / enough feedback /
    dismissed). The inverse of the graduation condition."""
    if store.get_setting("onboard_dismissed") == "1":
        return False
    if not _has_synced(store):
        return False
    if _taste_ready(store):
        return False
    if feedback_count(store) >= rec_params.get_param(store, "onboard_feedback_min"):
        return False
    return True


def library_sample(store, n=12) -> list[dict]:
    """A genre-diverse sample of owned tracks: round-robin across genre families, so ratings teach
    breadth. Falls back to any owned tracks when genres are sparse."""
    rows = store.conn.execute(
        "SELECT identity_key k, MIN(title) title, MIN(artist) artist, MIN(album) album, "
        "MIN(video_id) vid, MIN(thumbnail) thumb, MIN(genre) genre FROM tracks "
        "WHERE video_id IS NOT NULL AND video_id<>'' GROUP BY identity_key").fetchall()
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
