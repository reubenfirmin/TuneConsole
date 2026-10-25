"""Worker-prepared intensity bands across complete radio style membership."""
from collections import defaultdict

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import musical_character

VERSION = 1
FIT_STEP = .04
RESOLUTION = 50


def loss(value, target):
    """A neutral midpoint; stronger requests distinguish finer suitability bands."""
    return round(2 * abs(target - .5) * abs(value - target) / FIT_STEP)


def narrow(items, target):
    if not items or target == .5:
        return items
    best = min(loss(musical_character.intensity(item), target) for item in items)
    return [item for item in items if loss(musical_character.intensity(item), target) == best]


def prepare(store, catalog, profiles, metadata):
    members = catalog.get('members') or {}
    missing = [key for keys in members.values() for key in keys if key not in profiles]
    profiles = {**profiles, **musical_character.load(store, missing)}
    keys = list(dict.fromkeys(key for keys in members.values() for key in keys))
    genres = {}
    for start in range(0, len(keys), 512):
        check_cancellable_work()
        genres.update(store.rec_query.track_genres(keys[start:start + 512]))
    clusters = {}
    for mid, keys in members.items():
        bands = defaultdict(lambda: defaultdict(list))
        for key in keys:
            check_cancellable_work()
            value = musical_character.intensity({'musical': profiles.get(key)})
            label = genres.get(key) or metadata.get(key, {}).get('genre') or ''
            bands[str(round(value * RESOLUTION))][label].append(key)
        clusters[mid] = {band: dict(groups) for band, groups in bands.items()}
    catalog['intensity'] = {'version': VERSION, 'profile_version': musical_character.VERSION,
                            'clusters': clusters}


def ordered_groups(bands, target):
    """Only small band/genre headings are ranked on a control change."""
    for band in sorted(bands, key=lambda band: (loss(int(band) / RESOLUTION, target),
                                               abs(int(band) / RESOLUTION - target))):
        yield loss(int(band) / RESOLUTION, target), bands[band]
