"""On-demand discovery for a Fresh card whose requested genres have too few candidates."""
from collections import Counter, deque
from contextlib import nullcontext

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.accounts import catalog_client

from yt_playlist.rec import embedding_vectors as vectors, home_themes, home_vibe, measurement, mode_surfaces, rec_params, road_trip_candidates, surfaces
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


def populate(ctx, include_genres=(), avoid_genres=(), on_progress=lambda message: None):
    store = ctx.store
    client = catalog_client(ctx.client_provider())
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
        genre = labels.get(family) or next(iter(genre_map.subgenres_of(family,conn=store.conn)), family.removeprefix("other:"))
        on_progress(f"Finding fresh {genre} songs…")
        # Resolve the genre to known artists, then fetch their songs. A text search for a genre
        # would return mixes and uploads whose titles merely contain those words.
        for artist in road_trip_candidates.genre_artists(store, genre, limit=6):
            if artist in muted:
                continue
            rows, _ = road_trip_candidates.other_input_songs(client, "artist", artist, 4, store)
            for row in rows:
                title, name = row.get("title"), row.get("artist")
                if not title or not name or not row.get("video_id"):
                    continue
                key = store.tracks.identity_key_for_video(row["video_id"])
                if key in owned or key in have or key in suppressed or name in muted:
                    continue
                actual_genre = row.get("genre") or genre
                actual_family = genre_map.family(actual_genre,conn=store.conn)
                if actual_family in avoid_genres or (include_genres and actual_family not in include_genres):
                    continue
                key = store.discovery.upsert_discovered_track(
                    identity_key(title, name), row["video_id"], title, name, row.get("album"), row.get("thumbnail"),
                    actual_genre, None, f"genre:{family}", ctx.now_fn())
                have.add(key)
                added += 1
        on_progress(f"Found {added} new songs. Preparing your mix…")
    vectors.build_discovered_content_vectors(store)
    return added


def prepare(store, now):
    """Refresh both Fresh caches in the discovery/enrichment job, never during rendering."""
    return mode_surfaces.prepare_bundles(store, now)


def pick(store, now, include_genres=(), avoid_genres=(), size=12, *, minimum=4, bundles=None):
    """A balanced Fresh preview from already ranked candidates.

    Do not roll another single-genre theme over the user's selection. Each requested family must
    have candidates before the preview is ready; missing families keep the discovery slot visible.
    Reading a page or choosing genres must never fit a projection or score the discovery pool.
    Individual discovery tiles can use a smaller minimum than a saveable mix.
    """
    if preset := home_vibe.current(store, now):
        from yt_playlist.rec import vibe_pools
        tracks = vibe_pools.pick(store, now, preset, 'fresh', size=size,
                                 include_genres=include_genres, avoid_genres=avoid_genres)
        return tracks if len(tracks) >= minimum else []
    if bundles is None and getattr(store, '_recommendation_files', False):
        return _stored_pick(store, now, include_genres, avoid_genres, size, minimum)
    if bundles is None:
        bundles = store.rec_proposals.get_proposals("mode_bundles") or {}
    candidates = list(store.rec_proposals.get_proposals("fresh_pool") or [])
    candidates.extend(bundles.get("all", {}).get("fresh", []))
    for mid, bucket in bundles.items():
        if mid != "all" and not mid.startswith("_"):
            candidates.extend(bucket.get("fresh", []))
    if not candidates:
        return []
    dao = store.rec_query
    keys = [t['key'] for t in candidates if t.get('key')]
    blocked = set(dao.tracks_by_keys(keys)) | store.rec_eligibility.suppressed_candidate_keys(keys, now)
    muted = store.rec_model.muted_artists()
    weights = store.rec_model.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    unique = {}
    for track in candidates:
        key = track.get("key")
        if key and track.get("video_id") and key not in blocked and track.get("artist") not in muted:
            unique.setdefault(key, track)
    pool = (home_themes.matching_genres(store, list(unique.values()), include=include_genres, avoid=avoid_genres)
            if include_genres or avoid_genres else home_themes.resolved_genres(store, list(unique.values())))
    pool = [t for t in pool if weights.get(f"genre:{genre_map.family(t.get('genre'),conn=store.conn)}", 1.0) != 0]
    pool = home_vibe.rank(store, now, pool)
    buckets = ({g: deque(t for t in pool if genre_map.family(t["genre"],conn=store.conn) == g) for g in include_genres}
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
    if len(picked) < minimum or not set(include_genres) <= {genre_map.family(t["genre"],conn=store.conn) for t in picked}:
        return []
    return picked


def _stored_pick(store, now, include_genres, avoid_genres, size, minimum):
    """Keep one rank page per requested family and only the selected preview.

    Exhausting one family continues through its complete ranked membership;
    neither a global head nor another prolific artist can hide its tail.
    """
    if (type(size) is not int or not 1 <= size <= 512
            or len(include_genres) > 128 or len(avoid_genres) > 128):
        raise ValueError('Invalid bounded Fresh preview')
    with (nullcontext(store) if getattr(store, '_read_only', False) else store.read_view()) as reader:
        slots = []
        head = reader.recommendations.head('fresh')
        if head and reader.recommendations.pool('fresh', 'all', 'fresh', generation=head['id']):
            slots.append(('fresh', 'all', head['id']))
        head = reader.recommendations.head('home')
        if head:
            modes = reader.recommendations.selections('home', 'fresh', generation=head['id'])
            modes.sort(key=lambda value: (value['mode_key'] != 'all', value['mode_key']))
            slots.extend(('home', value['mode_key'], head['id']) for value in modes)
        if not slots:
            return []
        muted = reader.rec_model.muted_artists()
        weights = reader.rec_model.get_weights(now=now,
            revert_halflife_d=rec_params.get_param(reader, 'weight_revert_halflife_d'))
        artist_cap = int(rec_params.get_param(reader, 'modes_artist_cap'))

        def candidates(family):
            for surface, mode, generation in slots:
                after = -1
                while True:
                    check_cancellable_work()
                    page = reader.recommendations.candidate_page(surface, mode, 'fresh',
                        generation=generation, after=after, limit=128)
                    tracks = page['tracks']
                    keys = [track['key'] for track in tracks]
                    blocked = reader.rec_eligibility.suppressed_candidate_keys(keys, now)
                    tracks = home_themes.matching_genres(reader, tracks,
                        include=(family,) if family is not None else (), avoid=avoid_genres)
                    for track in tracks:
                        if (track['_radio_fresh'] and track['key'] not in blocked
                                and track.get('artist') not in muted
                                and weights.get(f"genre:{genre_map.family(track.get('genre'),conn=reader.conn)}", 1.0) != 0):
                            yield track
                    if page['exhausted']:
                        break
                    after = page['after']

        families = list(dict.fromkeys(include_genres)) or [None]
        streams = {family: iter(candidates(family)) for family in families}
        artists, seen, picked = Counter(), set(), []
        while streams and len(picked) < size:
            for family, stream in list(streams.items()):
                for track in stream:
                    if track['key'] in seen or artists[track.get('artist')] >= artist_cap:
                        continue
                    seen.add(track['key'])
                    artists[track.get('artist')] += 1
                    picked.append(track)
                    break
                else:
                    del streams[family]
                if len(picked) == size:
                    break
        if (len(picked) < minimum
                or not set(include_genres) <= {genre_map.family(track.get('genre'),conn=reader.conn) for track in picked}):
            return []
        return picked
