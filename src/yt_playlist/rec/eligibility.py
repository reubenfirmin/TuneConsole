"""Current exclusions shared by fresh assembly and bounded prepared-offer validation."""


def comfort_tracks(store, tracks, now):
    """Recheck listening and favorite status for the bounded prepared selection."""
    from yt_playlist.rec import candidate_policy, rec_params
    keys = tuple(sorted({track['key'] for track in tracks}))
    if not keys:
        return []

    # Listening and likes can be reused across visits. Evaluate the moving
    # rest cutoff on every response against the same committed inputs.
    facts = store.memoized_read(('comfort-facts', keys), lambda: store.rec_candidates.comfort_facts(keys))
    policy = candidate_policy.comfort_criteria(now, rec_params.get_param(store, 'comfort_min_plays'),
        rec_params.get_param(store, 'comfort_recency_full_days'))
    eligible = {row['key'] for row in facts if (row['liked'] or row['plays'] >= policy.repeated_plays)
                and (row['last_played'] is None or row['last_played'] <= policy.month)}
    return [track for track in tracks if track['key'] in eligible]


def home_exclusions(store, keys, now, *, check_genres=True):
    """Feedback and canonical artist/genre bans.

    Pass None to share the current exclusions across lanes on the same reader.
    Lane-specific ownership and requested genres use the caller's candidate metadata.
    """
    if keys is None:
        return store.memoized_read(('home-exclusions', now, check_genres),
            lambda: store.rec_model.suppressed_keys('for_you', now, check_genres=check_genres))
    keys = set(keys)
    if not keys:
        return set()
    return store.rec_model.suppressed_keys("for_you", now, keys=keys, check_genres=check_genres)
