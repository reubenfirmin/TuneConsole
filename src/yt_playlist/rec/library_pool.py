"""Library pools built only after clicking an unfilled Home mix."""
from yt_playlist.rec import home_themes, home_vibe, mode_surfaces, rec_params, recommend
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_map


def prepare(store, now, parameters):
    lane = parameters["lane"]
    keys = RecDao(store).library_keys()
    if lane == "temporal":
        keys = sorted(keys)
        meta = store.tracks_by_keys(keys)
        genres, years = store.modes.genres_for(keys), store.modes.years_for(keys)
        tracks = [mode_surfaces._temporal_item(k, meta, genres, years) for k in keys]
    else:
        scorer = {"wheelhouse": recommend.for_you, "explore": recommend.explore_for_you,
                  "comfort": recommend.comfort_listening}[lane]
        # Filter after ranking the whole library, rather than a small global head that may
        # contain none of the requested genres. This work runs in the clicked background job.
        tracks = [mode_surfaces._item_dict(t) for t in scorer(store, now, limit=max(300, len(keys)))]
    tracks = home_themes.matching_genres(store, tracks, include=parameters["include_genres"],
                                        avoid=parameters["avoid_genres"])
    store.put_proposals(f"home_pool:{lane}", {"parameters": parameters, "tracks": tracks}, now)


def pick(store, now, parameters, exclude_keys=()):
    cached = store.get_proposals(f"home_pool:{parameters['lane']}") or {}
    if cached.get("parameters") != parameters:
        return []
    dao = RecDao(store)
    blocked = set(exclude_keys) | dao.generated_track_keys() | store.suppressed_keys("for_you", now)
    owned, muted = dao.library_keys(), store.muted_artists()
    weights = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    tracks = home_themes.matching_genres(store, cached["tracks"], include=parameters["include_genres"],
                                        avoid=parameters["avoid_genres"])
    tracks = [t for t in tracks if t["key"] in owned and t["key"] not in blocked
              and t["artist"] not in muted
              and weights.get(f"genre:{genre_map.family(t.get('genre'))}", 1.0) != 0]
    if parameters["lane"] == "temporal":
        cuts = (store.get_proposals("mode_bundles") or {}).get("_meta", {}).get("year_cuts")
        if not cuts:
            return []
        tracks = [t for t in tracks if mode_surfaces._in_band(t.get("year"), parameters["epoch"] % 3,
                                                             cuts[0], cuts[1])]
    tracks = home_vibe.rank(store, now, tracks)
    return mode_surfaces._diversify(tracks, int(rec_params.get_param(store, "modes_artist_cap")),
                                    int(rec_params.get_param(store, "modes_album_cap")))
