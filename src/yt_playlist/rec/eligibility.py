"""Current exclusions shared by fresh assembly and bounded prepared-offer validation."""


def comfort_tracks(store, tracks, now):
    """Recheck listening and favorite status for the bounded prepared selection."""
    from yt_playlist.rec import rec_params
    keys = {track['key'] for track in tracks}
    if not keys:
        return []
    eligible = {row['key'] for row in store.rec_candidates.comfort_candidates(now,
        min_plays=rec_params.get_param(store, 'comfort_min_plays'),
        recency_full_days=rec_params.get_param(store, 'comfort_recency_full_days'),
        keys=keys, limit=len(keys))}
    return [track for track in tracks if track['key'] in eligible]


def home_exclusions(store, keys, now):
    """Feedback and canonical artist/genre bans.

    Keep this bounded to offered candidates. Lane-specific ownership and requested genre
    constraints are applied by the caller against its current candidate metadata.
    """
    keys = set(keys)
    if not keys:
        return set()
    return (store.rec_eligibility.suppressed_candidate_keys(keys, now)
            | store.rec_model.suppressed_keys("for_you", now, keys=keys))
