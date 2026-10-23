"""Current exclusions shared by fresh assembly and bounded prepared-offer validation."""


def home_exclusions(store, keys, now):
    """Feedback, canonical artist/genre bans and generated-playlist quarantine.

    Keep this bounded to offered candidates. Lane-specific ownership and requested genre
    constraints are applied by the caller against its current candidate metadata.
    """
    keys = set(keys)
    if not keys:
        return set()
    return (store.rec_eligibility.suppressed_candidate_keys(keys, now)
            | store.rec_model.suppressed_keys("for_you", now, keys=keys))
