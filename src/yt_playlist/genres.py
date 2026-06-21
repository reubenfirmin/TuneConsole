"""A curated whitelist of music genres, used to pin a clean genre from noisy Last.fm tags.

Last.fm tags are a folksonomy: alongside real genres you get moods, decades, "seen live", "favourite
songs", artist names, etc. We can't trust the top tag blindly. Instead we match tags (highest count
first) against this whitelist and take the first hit, so the result is always a recognized genre.

Each entry is `(Display Name, [extra aliases])`. Matching is done on a normalized form (lowercase,
alphanumerics only), so "Hip-Hop", "hip hop" and "hiphop" all map to one entry — list aliases only
for genuinely different spellings (abbreviations, &-vs-and, etc.).
"""

_GENRES = [
    # --- rock ---
    ("Rock", []),
    ("Classic Rock", []),
    ("Hard Rock", []),
    ("Soft Rock", []),
    ("Indie Rock", []),
    ("Indie", []),
    ("Alternative Rock", ["alternative", "alt rock"]),
    ("Punk", ["punk rock"]),
    ("Pop Punk", []),
    ("Post-Punk", []),
    ("Post-Rock", []),
    ("Psychedelic Rock", ["psychedelic", "psych rock"]),
    ("Progressive Rock", ["prog rock", "prog"]),
    ("Art Rock", []),
    ("Garage Rock", ["garage"]),
    ("Surf Rock", []),
    ("Glam Rock", ["glam"]),
    ("Grunge", []),
    ("Shoegaze", []),
    ("Math Rock", []),
    ("Noise Rock", []),
    ("Krautrock", []),
    ("Southern Rock", []),
    ("Folk Rock", []),
    ("Blues Rock", []),
    ("Stoner Rock", ["stoner"]),
    ("Emo", []),
    ("Rockabilly", []),
    # --- metal ---
    ("Metal", ["heavy metal"]),
    ("Death Metal", []),
    ("Black Metal", []),
    ("Doom Metal", ["doom"]),
    ("Thrash Metal", ["thrash"]),
    ("Power Metal", []),
    ("Progressive Metal", ["prog metal"]),
    ("Nu Metal", ["nu-metal"]),
    ("Metalcore", []),
    ("Deathcore", []),
    ("Folk Metal", []),
    ("Industrial Metal", []),
    ("Symphonic Metal", []),
    ("Sludge", ["sludge metal"]),
    ("Grindcore", []),
    ("Hardcore", ["hardcore punk"]),
    # --- pop ---
    ("Pop", []),
    ("Synth-Pop", ["synthpop"]),
    ("Electropop", []),
    ("Indie Pop", []),
    ("Dream Pop", []),
    ("Power Pop", []),
    ("K-Pop", ["kpop"]),
    ("J-Pop", ["jpop"]),
    ("Dance-Pop", ["dance pop"]),
    ("Art Pop", []),
    ("Bedroom Pop", []),
    ("Hyperpop", []),
    ("Baroque Pop", []),
    ("Teen Pop", []),
    # --- electronic ---
    ("Electronic", ["electronica", "electro"]),
    ("House", []),
    ("Deep House", []),
    ("Tech House", []),
    ("Progressive House", []),
    ("Electro House", []),
    ("Techno", []),
    ("Trance", []),
    ("Psytrance", ["psychedelic trance"]),
    ("Drum & Bass", ["drum and bass", "drum n bass", "dnb", "d&b"]),
    ("Dubstep", []),
    ("Garage", ["uk garage"]),
    ("Breakbeat", ["breaks"]),
    ("Jungle", []),
    ("Ambient", []),
    ("Downtempo", []),
    ("Trip Hop", []),
    ("IDM", ["intelligent dance music"]),
    ("EDM", []),
    ("Big Room", []),
    ("Future Bass", []),
    ("Synthwave", ["retrowave", "outrun"]),
    ("Vaporwave", []),
    ("Chillwave", []),
    ("Chillout", ["chill"]),
    ("Lo-Fi", ["lofi"]),
    ("Glitch", []),
    ("Hardstyle", []),
    ("Gabber", []),
    ("Eurodance", []),
    ("Acid", ["acid house"]),
    ("Minimal", ["minimal techno"]),
    ("Disco", []),
    ("Nu-Disco", ["nu disco"]),
    # --- hip hop / r&b ---
    ("Hip Hop", ["hip-hop", "rap"]),
    ("Trap", []),
    ("Boom Bap", []),
    ("Conscious Hip Hop", []),
    ("Gangsta Rap", []),
    ("Cloud Rap", []),
    ("Drill", []),
    ("Grime", []),
    ("R&B", ["rnb", "r and b", "rhythm and blues"]),
    ("Contemporary R&B", []),
    ("Neo-Soul", ["neo soul"]),
    ("Soul", []),
    ("Funk", []),
    ("Motown", []),
    ("Gospel", []),
    ("New Jack Swing", []),
    # --- jazz / blues ---
    ("Jazz", []),
    ("Smooth Jazz", []),
    ("Bebop", []),
    ("Swing", []),
    ("Big Band", []),
    ("Cool Jazz", []),
    ("Jazz Fusion", ["fusion"]),
    ("Free Jazz", []),
    ("Acid Jazz", []),
    ("Bossa Nova", []),
    ("Blues", []),
    ("Delta Blues", []),
    ("Chicago Blues", []),
    # --- folk / country / acoustic ---
    ("Folk", []),
    ("Indie Folk", []),
    ("Folk Pop", []),
    ("Singer-Songwriter", ["singer songwriter"]),
    ("Americana", []),
    ("Country", []),
    ("Alt-Country", ["alt country"]),
    ("Bluegrass", []),
    ("Country Rock", []),
    ("Acoustic", []),
    ("Celtic", []),
    # --- reggae / caribbean / latin ---
    ("Reggae", []),
    ("Dub", []),
    ("Ska", []),
    ("Dancehall", []),
    ("Reggaeton", []),
    ("Latin", []),
    ("Salsa", []),
    ("Bachata", []),
    ("Cumbia", []),
    ("Bossa", []),
    ("Samba", []),
    ("Flamenco", []),
    ("Afrobeat", ["afrobeats"]),
    ("Soca", []),
    # --- world / other ---
    ("World", ["world music"]),
    ("Classical", []),
    ("Opera", []),
    ("Baroque", []),
    ("Soundtrack", ["score", "film score", "ost"]),
    ("New Age", []),
    ("Spoken Word", []),
    ("Industrial", []),
    ("EBM", []),
    ("Gothic", ["goth"]),
    ("Darkwave", []),
    ("Experimental", ["avant-garde", "avantgarde"]),
    ("Noise", []),
    ("Drone", []),
    ("Post-Hardcore", []),
    ("Math Pop", []),
    ("Christian", ["christian rock", "ccm"]),
    ("Holiday", ["christmas"]),
]


def _normalize(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


_BUILTIN_ALIASES = {d: a for d, a in _GENRES}          # display name -> [extra aliases]
_BUILTIN_NAMES = [d for d, _ in _GENRES]


def build_canon(names):
    """Map normalized-tag -> canonical display name for the given active genre names. Built-in names
    also pull in their known aliases, so variant spellings still match (e.g. 'hiphop' -> 'Hip Hop')."""
    canon = {}
    for name in names:
        canon[_normalize(name)] = name
        for alias in _BUILTIN_ALIASES.get(name, ()):
            canon[_normalize(alias)] = name
    return canon


_BUILTIN_CANON = build_canon(_BUILTIN_NAMES)
_active = None                                          # set via configure(); None = use built-in


def builtin_names():
    return sorted(_BUILTIN_NAMES, key=str.lower)


def set_active(names):
    """Replace the active whitelist (a list of display names), or None to fall back to built-in."""
    global _active
    _active = build_canon(names) if names is not None else None


def configure(store):
    """Load the active whitelist from the store, seeding the built-in list on first run."""
    if not store.get_setting("genres_seeded"):
        store.set_genres(_BUILTIN_NAMES)
        store.set_setting("genres_seeded", "1")
    set_active(store.get_genre_whitelist())


def _canon():
    return _active if _active is not None else _BUILTIN_CANON


def all_genres() -> list:
    """The active canonical genre display names, alpha-sorted (for autosuggest)."""
    return sorted(set(_canon().values()), key=str.lower)


def match_tag(tag):
    """Return the canonical genre for a single tag, or None if it isn't a recognized genre."""
    return _canon().get(_normalize(tag))


def pick_genre(tags):
    """Given tags most-relevant-first (strings), return the first that maps to a known genre."""
    for tag in tags:
        hit = match_tag(tag)
        if hit:
            return hit
    return None
