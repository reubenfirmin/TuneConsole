"""On-demand discovery for a Fresh card whose requested genres have too few candidates."""
from collections import Counter, deque

from yt_playlist.rec import embed, home_themes, home_vibe, measurement, mode_surfaces, rec_params, road_trip, surfaces
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


def populate(ctx, include_genres=(), avoid_genres=(), on_progress=lambda message: None):
    store = ctx.store
    client = next(iter((ctx.client_provider() or {}).values()), None)
    if client is None:
        raise ValueError("Connect YouTube Music to find fresh songs.")
    options = home_themes.genre_options(store)
    labels = {g["family"]: g["label"] for g in options}
    genres = sorted(set(include_genres)) or [g["family"] for g in options if g["family"] not in avoid_genres][:6]
    if not genres:
        raise ValueError("Choose some genres to find fresh songs.")
    owned = store.rec_query.library_keys()
    have = {t["identity_key"] for t in store.discovery.get_discovered_tracks()}
    suppressed = store.rec_model.suppressed_keys("for_you", ctx.now_fn())
    muted = store.rec_model.muted_artists()
    added = 0
    for family in genres:
        # Internal families such as 'dnb' and 'rock-alt' are not searchable genre names.
        genre = labels.get(family) or next(iter(genre_map.subgenres_of(family)), family.removeprefix("other:"))
        on_progress(f"Finding fresh {genre} songs…")
        # Resolve the genre to known artists, then fetch their songs. A text search for a genre
        # would return mixes and uploads whose titles merely contain those words.
        for artist in road_trip.genre_artists(store, genre, limit=6):
            if artist in muted:
                continue
            rows, _ = road_trip.other_input_songs(client, "artist", artist, 4, store)
            for row in rows:
                title, name = row.get("title"), row.get("artist")
                if not title or not name or not row.get("video_id"):
                    continue
                key = store.tracks.identity_key_for_video(row["video_id"])
                if key in owned or key in have or key in suppressed or name in muted:
                    continue
                actual_genre = row.get("genre") or genre
                actual_family = genre_map.family(actual_genre)
                if actual_family in avoid_genres or (include_genres and actual_family not in include_genres):
                    continue
                key = store.discovery.upsert_discovered_track(
                    identity_key(title, name), row["video_id"], title, name, row.get("album"), row.get("thumbnail"),
                    actual_genre, None, f"genre:{family}", ctx.now_fn())
                have.add(key)
                added += 1
        on_progress(f"Found {added} new songs. Preparing your mix…")
    embed.build_discovered_content_vectors(store)
    return added


def prepare(store, now):
    """Refresh both Fresh caches in the discovery/enrichment job, never during rendering."""
    pool = surfaces.cold_candidates(store, now, limit=None)
    store.rec_surface.put_proposals("fresh_pool", measurement.stamp_pool(
        store, [surfaces._item_to_fresh_dict(t) for t in pool], now, "cold"), now)
    mode_surfaces.refresh_fresh(store, pool, now)


def pick(store, now, include_genres=(), avoid_genres=(), size=12):
    """A balanced Fresh preview from already ranked candidates.

    Do not roll another single-genre theme over the user's selection. Each requested family must
    have candidates before the preview is ready; missing families keep the discovery slot visible.
    Reading a page or choosing genres must never fit a projection or score the discovery pool.
    """
    bundles = store.rec_surface.get_proposals("mode_bundles") or {}
    candidates = list(store.rec_surface.get_proposals("fresh_pool") or [])
    candidates.extend(bundles.get("all", {}).get("fresh", []))
    for mid, bucket in bundles.items():
        if mid != "all" and not mid.startswith("_"):
            candidates.extend(bucket.get("fresh", []))
    if not candidates:
        return []
    dao = store.rec_query
    blocked = dao.library_keys() | set(dao.generated_track_keys()) | store.rec_model.suppressed_keys("for_you", now)
    muted = store.rec_model.muted_artists()
    weights = store.rec_model.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    unique = {}
    for track in candidates:
        key = track.get("key")
        if key and key not in blocked and track.get("artist") not in muted:
            unique.setdefault(key, track)
    pool = (home_themes.matching_genres(store, list(unique.values()), include=include_genres, avoid=avoid_genres)
            if include_genres or avoid_genres else home_themes.resolved_genres(store, list(unique.values())))
    pool = [t for t in pool if weights.get(f"genre:{genre_map.family(t.get('genre'))}", 1.0) != 0]
    pool = home_vibe.rank(store, now, pool)
    buckets = ({g: deque(t for t in pool if genre_map.family(t["genre"]) == g) for g in include_genres}
               if include_genres else {"all": deque(pool)})
    if not buckets or any(not b for b in buckets.values()):
        return []
    artist_cap = int(rec_params.get_param(store, "modes_artist_cap"))
    artists, seen, picked = Counter(), set(), []
    while any(buckets.values()) and len(picked) < size:
        for bucket in buckets.values():
            while bucket:
                track = bucket.popleft()
                if track["key"] in seen or artists[track["artist"]] >= artist_cap:
                    continue
                seen.add(track["key"])
                artists[track["artist"]] += 1
                picked.append(track)
                break
            if len(picked) >= size:
                break
    if len(picked) < 4 or not set(include_genres) <= {genre_map.family(t["genre"]) for t in picked}:
        return []
    return picked
