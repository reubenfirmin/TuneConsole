"""Library pools built only after clicking an unfilled Home mix."""
from yt_playlist.rec import home_themes, home_vibe, measurement, mode_surfaces, rec_params, surfaces


def prepare(store, now, parameters):
    if home_vibe.current(store, now):
        # A clicked fallback joins the ordinary worker's hourly admission.
        return mode_surfaces.prepare_scheduled(store, now)
    if store._clicked_library_pools:
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            tracks = _calculate(reader, now, parameters)
            policies = measurement.PoolPolicies(reader, now)
            tracks = policies.stamp(tracks, parameters['lane'])
            policy = policies.generation_policy()
            rows, bases = list(policies.rows.values()), list(policies.bases.values())
            surface = 'home_pool:' + parameters['lane']
            plan = reader.recommendations.prepare_clicked_pool(surface,
                {'parameters': parameters, 'tracks': tracks}, now, policy=policy,
                input_revision=reader.prepared_content.serial(), pending_policies=rows)
            snapshot.release_reader()
            store.rec_ledger.record_prepared_policies(rows, bases)
            build = store.recommendations.claim(plan)
            store.recommendations.remember_clicked_pool_parameters(build.generation_id, parameters, now)
            store.recommendations.stage(plan, build)
            return snapshot.publish_ordered(lambda: store.recommendations.publish(build, now), validate_inputs=False)
    tracks = _calculate(store, now, parameters)
    tracks = measurement.stamp_pool(store, tracks, now, parameters['lane'])
    store.rec_proposals.put_proposals(f"home_pool:{parameters['lane']}", {"parameters": parameters, "tracks": tracks}, now)


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
    axes = store.genres.feature_axes(t.get('genre') for t in tracks)
    tracks = [t for t in tracks if t["key"] in owned and t["key"] not in blocked
              and t["artist"] not in muted
              and weights.get(f"genre:{axes.get(t.get('genre'), ('', None))[0]}", 1.0) != 0]
    if parameters["lane"] == "temporal":
        cuts = mode_surfaces.bundle_metadata(store).get('year_cuts')
        if not cuts:
            return []
        tracks = [t for t in tracks if mode_surfaces._in_band(t.get("year"), parameters["epoch"] % 3,
                                                             cuts[0], cuts[1])]
    tracks = home_vibe.rank(store, now, tracks)
    return mode_surfaces._diversify(tracks, int(rec_params.get_param(store, "modes_artist_cap")),
                                    int(rec_params.get_param(store, "modes_album_cap")))
