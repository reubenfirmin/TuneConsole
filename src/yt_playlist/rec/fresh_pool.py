"""On-demand discovery for a Fresh card whose requested genres have too few candidates."""
from contextlib import nullcontext

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import home_themes, home_vibe, mode_surfaces, pool_selection, rec_params


def populate(store, client, now, include_genres=(), avoid_genres=(), on_progress=lambda message: None):
    from yt_playlist.library.pool_composition import fresh
    return fresh(store).populate(client, now, include_genres, avoid_genres, on_progress)


def prepare(store, now):
    """Refresh both Fresh caches in the discovery/enrichment job, never during rendering."""
    return mode_surfaces.prepare_scheduled(store, now)


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
    unique = pool_selection.unique_fresh(candidates, blocked, muted)
    pool = (home_themes.matching_genres(store, unique, include=include_genres, avoid=avoid_genres)
            if include_genres or avoid_genres else home_themes.resolved_genres(store, unique))
    axes = store.genres.feature_axes(t.get('genre') for t in pool)
    pool = list(store.artists.credited_pages(pool))
    artist_cap = int(rec_params.get_param(store, "modes_artist_cap"))
    return pool_selection.fresh_values(pool, axes, weights, include_genres, size, minimum, artist_cap)


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
                    axes = reader.genres.feature_axes(track.get('genre') for track in tracks)
                    for track in tracks:
                        if (track['_radio_fresh'] and track['key'] not in blocked
                                and track.get('artist') not in muted
                                and weights.get(f"genre:{axes.get(track.get('genre'), ('', None))[0]}", 1.0) != 0):
                            yield track
                    if page['exhausted']:
                        break
                    after = page['after']

        families = list(dict.fromkeys(include_genres)) or [None]
        streams = {family: iter(candidates(family)) for family in families}
        picked = pool_selection.deal_streams(streams, size, artist_cap)
        axes = reader.genres.feature_axes(track.get('genre') for track in picked)
        if not pool_selection.complete_fresh(picked, axes, include_genres, minimum):
            return []
        return picked
