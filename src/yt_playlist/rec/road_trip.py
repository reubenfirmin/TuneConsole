"""Road Trip playlist generation: blend your taste-weighted tracks (the 'own' pool) with popular
tracks pulled from YouTube for other people's artists/genres (the 'other' pool), interleaved by a
target ratio and cut to a target duration. Neither pool needs bespoke taste-model handling: the
resulting playlist is materialized via executor.create_generated_playlist with the normal Generated
group, which already quarantines it from every taste signal and schedules it for GC.
"""
from yt_playlist.providers import deezer
from yt_playlist.rec import recommend, surfaces
from yt_playlist.rec.journeys import journey_order
from yt_playlist.util.thumbnails import best_thumb

ARTIST_SONGS_LIMIT = 15   # top songs pulled per artist input, before popularity sort/cap
GENRE_SEARCH_LIMIT = 15    # search results pulled per genre input, before popularity sort/cap
DIVERSITY_ARTIST_CAP = 3   # max tracks from one artist in the "other" pool


def build_own_pool(store, now, blacklist_genres, limit):
    """Taste-weighted tracks from your library (surfaces.for_you), excluding any track whose genre
    or genre family is in `blacklist_genres`. Returns normalized track dicts tagged source='mine'."""
    pool = surfaces.for_you(store, now, limit=limit)
    blocked = store.keys_in_genre_selection(blacklist_genres) if blacklist_genres else set()
    out = []
    for item in pool:
        if not item.video_id or (item.key and item.key in blocked):
            continue
        out.append({"video_id": item.video_id, "title": item.title, "artist": item.artist,
                    "album": item.album, "thumbnail": item.thumbnail, "duration": None,
                    "source": "mine"})
    return out


def _popularity(title, artist):
    """Deezer popularity rank for a candidate track, or None if unmatched/unavailable. Module-level
    so tests can patch it without a real network call (mirrors discover.fetch_artist_info's
    'module-level so tests can patch it' convention)."""
    return deezer.enrich(title, artist).get("popularity")


def _sort_by_popularity(candidates):
    """Sort candidates by Deezer popularity, descending. A candidate with no Deezer match (None)
    keeps its original relative order, placed after every popularity-known candidate: YouTube's own
    ranking is itself a popularity proxy, so an unmatched candidate is not discarded."""
    scored = [(c, _popularity(c["title"], c["artist"])) for c in candidates]
    known = sorted((cp for cp in scored if cp[1] is not None), key=lambda cp: -cp[1])
    unknown = [c for c, p in scored if p is None]
    return [c for c, _ in known] + unknown


def _artist_songs(client, name, limit=ARTIST_SONGS_LIMIT):
    """Top songs for an artist, via YouTube Music's own artist page (already popularity-ranked by
    YouTube). Returns normalized track dicts tagged source='theirs', or [] if unresolvable.

    artist["songs"]["results"] rows go through ytmusicapi's parse_playlist_item (browsing.py:300 ->
    parsers/playlists.py), NOT the simplified shape shown in get_artist's own docstring: "artists"
    is a LIST of {"name","id"} dicts (parse_song_artists), and "album" is a {"name","id"} dict or
    None (parse_song_album) - never a plain string. duration_seconds is present when a duration was
    found, same field name as search results."""
    try:
        results = client.search(name, filter="artists") or []
    except Exception:  # noqa: BLE001 - network/parse all degrade to "no songs found"
        return []
    browse_id = results[0].get("browseId") if results else None
    if not browse_id:
        return []
    try:
        artist = client.get_artist(browse_id) or {}
    except Exception:  # noqa: BLE001
        return []
    rows = ((artist.get("songs") or {}).get("results")) or []
    out = []
    for row in rows[:limit]:
        vid = row.get("videoId")
        if not vid:
            continue
        artists = row.get("artists") or []
        artist_name = artists[0].get("name") if artists else None
        album = (row.get("album") or {}).get("name") or ""
        out.append({"video_id": vid, "title": row.get("title") or "",
                    "artist": artist_name or name, "album": album,
                    "thumbnail": best_thumb(row.get("thumbnails")),
                    "duration": row.get("duration_seconds"), "source": "theirs"})
    return out


def _genre_songs(client, genre, limit=GENRE_SEARCH_LIMIT):
    """Songs matching a genre term via YouTube Music search. No artist anchor exists for a genre
    input, so this searches the term directly rather than resolving via get_artist. Normalized the
    same as _artist_songs, but duration_seconds (present on search rows) is kept when available.

    Deliberately does not pass `limit=` through to client.search(): FakeClient.search() (the test
    double used throughout this suite) only accepts (query, filter), and the post-slice below
    already caps the result count, so nothing is lost by relying on that slice alone."""
    try:
        results = client.search(genre, filter="songs") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in results[:limit]:
        vid = row.get("videoId")
        if not vid:
            continue
        artists = row.get("artists") or []
        artist = artists[0].get("name") if artists else ""
        album = (row.get("album") or {}).get("name") or ""
        out.append({"video_id": vid, "title": row.get("title") or "", "artist": artist or "",
                    "album": album, "thumbnail": best_thumb(row.get("thumbnails")),
                    "duration": row.get("duration_seconds"), "source": "theirs"})
    return out


def build_other_pool(client, artists, genres, limit):
    """Popular tracks for other people's artists/genres: merged across every input, deduped by
    video_id, popularity-sorted, capped so no one artist dominates."""
    candidates, seen = [], set()
    for name in artists:
        for c in _artist_songs(client, name):
            if c["video_id"] not in seen:
                seen.add(c["video_id"])
                candidates.append(c)
    for g in genres:
        for c in _genre_songs(client, g):
            if c["video_id"] not in seen:
                seen.add(c["video_id"])
                candidates.append(c)
    candidates = _sort_by_popularity(candidates)
    per_artist, out = {}, []
    for c in candidates:
        n = per_artist.get(c["artist"], 0)
        if n >= DIVERSITY_ARTIST_CAP:
            continue
        per_artist[c["artist"]] = n + 1
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _track_duration_s(store, client, track):
    """Best-effort duration (seconds) for a track dict: what it already carries, else a duration
    known for the same song under any videoId, else one live lookup. Never raises; None on failure.
    (Re-implements executor._fetch_song_duration's lookup rather than importing that
    underscore-prefixed helper across modules.)"""
    if track.get("duration") is not None:
        return track["duration"]
    dur = store.known_duration(track["title"], track["artist"])
    if dur is not None:
        return dur
    try:
        details = (client.get_song(track["video_id"]) or {}).get("videoDetails") or {}
        secs = details.get("lengthSeconds")
        return int(secs) if secs not in (None, "") else None
    except Exception:  # noqa: BLE001 - duration is a nicety; never block generation
        return None


def assemble_playlist(store, client, recipe, now, seed=0):
    """Build the ordered, duration-budgeted track list for a Road Trip recipe. Returns
    (tracks, stats): tracks in final playback order, stats = {"achieved_minutes", "own_count",
    "their_count"}."""
    own_pct = recipe["own_pct"]
    target_s = recipe["target_minutes"] * 60
    pool_limit = max(40, recipe["target_minutes"] * 3)   # deep enough for a multi-hour mix
    own_pool = build_own_pool(store, now, recipe["blacklist_genres"], pool_limit)
    other_pool = build_other_pool(client, recipe["artists"], recipe["genres"], pool_limit)

    own_target = round(pool_limit * own_pct / 100)
    other_target = pool_limit - own_target
    combined = list(own_pool[:own_target]) + list(other_pool[:other_target])
    if len(other_pool) < other_target:                   # "theirs" ran short: top up from "mine"
        shortfall = other_target - len(other_pool)
        combined += own_pool[own_target:own_target + shortfall]

    combined = recommend.attach_genres(store, combined)

    def feat(it):
        return {"artist": it.get("artist") or "", "genre": it.get("genre") or "",
                "source": it.get("source") or "theirs"}

    ordered = journey_order(combined, "road_trip", seed, feat)

    tracks, total_s, own_count, their_count = [], 0.0, 0, 0
    for t in ordered:
        t["duration"] = _track_duration_s(store, client, t)
        tracks.append(t)
        total_s += t["duration"] or 0
        if t["source"] == "mine":
            own_count += 1
        else:
            their_count += 1
        if total_s >= target_s:
            break
    return tracks, {"achieved_minutes": round(total_s / 60, 1), "own_count": own_count,
                    "their_count": their_count}
