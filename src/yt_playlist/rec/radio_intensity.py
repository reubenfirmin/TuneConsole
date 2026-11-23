"""Worker-prepared intensity bands across complete On Air track group membership."""
import math
from collections import defaultdict

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import musical_character

VERSION = 3
RESOLUTION = 50
NEUTRAL = .5
FULL_ON = 'full_on'
CHILL = 'chill'


def preference(share, position, previous=None):
    """Spread the requested share evenly through positions after Apply."""
    if position is None:
        if share == NEUTRAL:
            return None  # Default startup before the listener applies tuning.
        position = 0
    full_on = math.floor((position + 1) * share + .5) > math.floor(position * share + .5)
    if full_on:
        return FULL_ON
    if share > NEUTRAL:
        previous = previous or {}
        if '_radio_intensity' in previous:
            score = previous['_radio_intensity']
        else:
            score = musical_character.intensity(previous)
        return band(NEUTRAL if score is None else score)
    return CHILL


def priority(value, preference):
    return band_priority(band(value), preference)


def band(value):
    return round(value * RESOLUTION)


def band_priority(value, preference):
    """Order a band by the slot's role in this mix."""
    if preference is None:
        return 0
    if preference == FULL_ON:
        return RESOLUTION - value
    if preference == CHILL:
        return value
    # Variety in an energetic mix favors the closest softer band first.
    return preference - value - 1 if value < preference else value


def narrow(items, preference):
    if not items or preference is None:
        return items
    ranked = [(priority(musical_character.intensity(item), preference), item) for item in items]
    best = min(value for value, _ in ranked)
    return [item for value, item in ranked if value == best]


def prepare(store, catalog, profiles, metadata):
    members = catalog.get('members') or {}
    missing = [key for keys in members.values() for key in keys if key not in profiles]
    profiles = {**profiles, **musical_character.load(store, missing)}
    keys = list(dict.fromkeys(key for keys in members.values() for key in keys))
    genres = {}
    for start in range(0, len(keys), 512):
        check_cancellable_work()
        genres.update(store.rec_query.track_genres(keys[start:start + 512]))
    catalog['intensity'] = intensity_bands(members, profiles, genres, metadata)


def intensity_bands(members, profiles, genres, metadata):
    """Plan bands from a captured membership, profile and genre snapshot."""
    clusters = {}
    for mid, keys in members.items():
        bands = defaultdict(lambda: defaultdict(list))
        for key in keys:
            value = musical_character.intensity({'musical': profiles.get(key)})
            label = genres.get(key) or metadata.get(key, {}).get('genre') or ''
            bands[str(band(value))][label].append(key)
        clusters[mid] = {band: dict(groups) for band, groups in bands.items()}
    return {'version': VERSION, 'profile_version': musical_character.VERSION, 'clusters': clusters}


def ordered_bands(bands, preference):
    """Only small band/genre headings are ranked on a control change."""
    rank = lambda value: band_priority(int(value), preference)
    for band in sorted(bands, key=rank):
        yield rank(band), bands[band]


def rank_prepared_track_groups(prepared, order, preference, criteria, *, limit, eclectic, contrast):
    """Rank eligible track groups by intensity bounds and optional genre contrast."""
    genre_allowed = criteria['genre_allowed']
    headings = prepared.intensity_headings()
    priorities = [band_priority(band, preference) for band in range(RESOLUTION + 1)]
    neutral = priority(NEUTRAL, preference)
    bounds = []
    for position, mid in enumerate(order):
        bands = headings.get(mid)
        lower = (min((priorities[band] for band, labels in bands.items() if any(map(genre_allowed, labels))),
                     default=None) if bands else neutral)
        if lower is not None:
            bounds.append((lower, position, mid))

    options = []
    cutoff = None
    for lower, position, mid in sorted(bounds):
        if cutoff and (lower > cutoff[0] or not eclectic and (lower, position) >= cutoff):
            break
        if mid in headings:
            tier, probe = prepared.intensity_pool(mid, preference, **criteria, limit=1,
                                                  max_priority=cutoff[0] if cutoff else None)
        else:
            # An unindexed track group keeps its bounded neutral path.
            probe = prepared.tracks('radio', mid, 'membership', allowed=criteria['allowed'],
                                    excluded=criteria['excluded'], used=criteria['used'], limit=1)
            tier = neutral
        if probe:
            options.append((tier, position, mid, probe[0]))
            if len(options) >= limit:
                options.sort()
                cutoff = options[limit - 1][:2]
    if eclectic and options and contrast is not None:
        best_fit = min(option[0] for option in options)
        options = [(priority, position, mid, prepared.contrast_witness(
            mid, preference, priority, witness, contrast=contrast, **criteria)
            if priority == best_fit else witness) for priority, position, mid, witness in options]
        options.sort(key=lambda option: (option[0], tuple(-value for value in contrast(option[3])),
                                         option[1]))
    else:
        options.sort()
    return options[:limit]
