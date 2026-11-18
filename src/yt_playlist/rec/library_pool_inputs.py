"""Library pools built only after clicking an unfilled Home mix."""
from yt_playlist.rec import home_themes, home_vibe, mode_rules as mode_surfaces, pool_selection, rec_params, surfaces


def _calculate(store, now, parameters):
    lane = parameters["lane"]
    keys = store.rec_query.library_keys()
    if lane == "temporal":
        keys = sorted(keys)
        meta = store.rec_query.tracks_by_keys(keys)
        genres, years = store.modes.genres_for(keys), store.modes.years_for(keys)
        tracks = [mode_surfaces._temporal_item(k, meta, genres, years) for k in keys]
    else:
        scorer = {"wheelhouse": surfaces.for_you, "explore": surfaces.explore_for_you,
                  "comfort": surfaces.comfort_listening}[lane]
        # Filter after ranking the whole library, rather than a small global head that may
        # contain none of the requested genres. This work runs in the clicked background job.
        tracks = [mode_surfaces._item_dict(t) for t in scorer(store, now, limit=max(300, len(keys)))]
    if not home_vibe.current(store, now):
        tracks = home_themes.matching_genres(store, tracks, include=parameters["include_genres"],
                                            avoid=parameters["avoid_genres"])
    return tracks


def pick(store, now, parameters, exclude_keys=()):
    if preset := home_vibe.current(store, now):
        from yt_playlist.rec import vibe_pools
        lane = parameters['lane']
        if lane == 'temporal':
            lane = f"temporal:{parameters['epoch'] % 3}"
        return vibe_pools.pick(store, now, preset, lane, exclude_keys,
                               include_genres=parameters['include_genres'], avoid_genres=parameters['avoid_genres'])
    cached = store.rec_proposals.get_proposals(f"home_pool:{parameters['lane']}") or {}
    if cached.get("parameters") != parameters:
        return []
    dao = store.rec_query
    blocked = set(exclude_keys) | store.rec_model.suppressed_keys("for_you", now)
    owned, muted = dao.library_keys(), store.rec_model.muted_artists()
    weights = store.rec_model.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    tracks = home_themes.matching_genres(store, cached["tracks"], include=parameters["include_genres"],
                                        avoid=parameters["avoid_genres"])
    if parameters['lane'] == 'comfort':
        from yt_playlist.rec.eligibility import comfort_tracks
        tracks = comfort_tracks(store, tracks, now)
    tracks = list(store.artists.credited_pages(tracks))
    axes = store.genres.feature_axes(t.get('genre') for t in tracks)
    cuts = mode_surfaces.bundle_metadata(store).get('year_cuts') if parameters['lane'] == 'temporal' else None
    return pool_selection.library_values(tracks, owned, blocked, muted, weights, axes, parameters, cuts,
        int(rec_params.get_param(store, 'modes_artist_cap')), int(rec_params.get_param(store, 'modes_album_cap')))
