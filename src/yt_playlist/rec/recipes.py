"""Generated-playlist recipes: roll a Home theme, build a Clusters mix recipe (#15), and theme-filter
candidates onto the rolled theme. The recipe records what a generated playlist IS so it can be re-run."""
import random
import zlib
from collections import Counter

from yt_playlist.util import genre_map
from yt_playlist.rec import arc_energy, embed, journeys, rec_params, transient
from yt_playlist.rec.ordering import _field
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.rec.taste_analysis import era_distribution, taste_breadth


# What each lane's tracks ARE to you - the relationship, which the content clause can't express.
_LANE_LEAD = {
    "wheelhouse": "Songs close to what you play most",
    "explore": "Songs you own but hardly ever play",
    "comfort": "Favorites you haven't reached for lately",
    "fresh": "Songs you don't own yet that fit your taste",
    # The temporal card is already a date band (Throwback / Time Flies / Recent Picks), so its lead
    # stays about the relationship and lets the era clause name the years.
    "temporal": "Songs from your library",
    "onboard_library": "Songs from your library",
    "onboard_radio": "Radio picks around your taste",
}
_DEFAULT_LEAD = "Songs picked for you"
_GENRE_FLOOR = 0.22      # a genre has to be at least this much of the card to be worth naming
_ERA_FLOOR = 0.25        # likewise a decade
_MOSTLY = 0.65           # one genre this dominant is described as "mostly X"
_COVERAGE = 0.4          # skip a clause unless this much of the card carries that data at all
# A facet has to be at least this much of your listening to be worth building a card around. The tail
# of a genre distribution is mostly bad tags carried in by one track ("other:dj", "other:likedis
# auto"), and avoiding the big families - which is what makes a row of cards differ - pushes rolls
# straight into it. Ignored if nothing clears the bar, so a thin library still rolls something.
_THEME_MIN_SHARE = 0.02


def _top_shares(counts, floor, limit=2, total=None):
    """The (name, share) pairs worth naming: biggest first, each at least `floor` of the whole."""
    total = total if total is not None else sum(counts.values())
    if not total:
        return []
    ranked = [(name, n / total) for name, n in counts.most_common() if name]
    return [(name, share) for name, share in ranked[:limit] if share >= floor]


def _join(names):
    return names[0] if len(names) == 1 else " and ".join(names)


def theme_counts(store, items):
    """Genre and decade counts behind the displayed description and the Home offer audit.
    Keep their metadata fallbacks identical so the audit explains what the user saw."""
    keys = [_field(it, "key") or "" for it in items]
    genres = Counter()
    by_artist = None
    for it in items:
        g = (_field(it, "genre") or "").strip()
        if not g:
            # Fall back to what this ARTIST's other tracks are tagged as. Enrichment runs behind the
            # library and doesn't reach the discovery pool at all, so the Fresh card is mostly
            # untagged tracks by artists you already own - and an artist's genre is a fair thing to
            # attribute to their song, where inventing one wouldn't be.
            if by_artist is None:
                by_artist = store.artist_genre_years()
            g = (by_artist.get(_field(it, "artist") or "") or {}).get("genre") or ""
        if g:
            genres[g.strip().lower()] += 1
    # Decades come from the library, except for tracks that aren't IN the library: the Fresh card's
    # proposals carry their own year, and are the whole reason that fallback exists.
    from_library = RecDao(store).track_decades(keys)
    decades = Counter()
    for it in items:
        own = _field(it, "year")
        d = (str(int(own) // 10 * 10) if own else None) or from_library.get(_field(it, "key") or "")
        if d:
            decades[d] += 1
    return genres, decades


def theme_sentence(store, model, items) -> str:
    """Describe the actual card's framing, genres and eras, rather than its intended recipe.
    Clauses are dropped when too few tracks carry metadata or no genre/decade dominates."""
    lead = _LANE_LEAD.get(model, _DEFAULT_LEAD)
    if not items:
        return lead + "."
    genres, decades = theme_counts(store, items)
    # Shares describe the whole card, including unknown tags. Sparse metadata cannot justify
    # "mostly trance", and two 2010s songs among six decades cannot describe the card's era.
    covered = lambda counts: sum(counts.values()) >= _COVERAGE * len(items)   # noqa: E731

    parts = [lead]
    named = _top_shares(genres, _GENRE_FLOOR, total=len(items)) if covered(genres) else []
    if named:
        # One genre carrying most of the card is "mostly X" even when a second scrapes the floor:
        # naming both would imply a balance ("trance and ambient" for three trance and one ambient).
        if named[0][1] >= _MOSTLY:
            parts.append(f"mostly {named[0][0]}")
        else:
            qualifier = "with " if sum(share for _, share in named) < 1 else ""
            parts.append(qualifier + _join([n for n, _ in named]))
    eras = _top_shares(decades, _ERA_FLOOR, total=len(items)) if covered(decades) else []
    era_share = sum(share for _, share in eras)
    if era_share >= _MOSTLY:
        tail = _join([f"{d}s" for d, _ in sorted(eras, key=lambda e: e[0])])
        parts.append(f"{'mostly ' if era_share < 1 else ''}from the {tail}")
    return (", ".join(parts[:2]) + (" " + parts[2] if len(parts) > 2 else "")) + "."


def pool_facets(store, items, min_tracks=3):
    """The genre-family and decade distributions of the candidates a card can actually draw from.

    A theme has to be rollable from THIS pool, not from your library at large: "From your catalog" is
    your under-played tail, which can be 77% one genre while your overall taste is nothing like that.
    Rolling against the global distribution asks for jazz from a pool that holds none, the filter
    finds no match, and the card fills with the pool's dominant genre - the same card every time,
    whatever the theme said. Families with fewer than `min_tracks` are dropped: a theme that can only
    fill one slot is not a theme."""
    fams, eras = Counter(), Counter()
    for it in items:
        genre = _field(it, "genre") or ""
        if genre:
            fams[genre_map.family(genre)] += 1
        year = _field(it, "year")
        if year:
            eras[str(int(year) // 10 * 10)] += 1
    def _shares(counts):
        kept = {k: n for k, n in counts.items() if k and n >= min_tracks}
        total = sum(kept.values())
        return {k: n / total for k, n in kept.items()} if total else {}
    return _shares(fams), _shares(eras)


def roll_recipe(store, model, seed=None, now=None, avoid=None, dists=None) -> dict:
    """Roll a per-playlist theme. Preference-weighted by your play distribution × permanent axis
    weights × the live transient facet leans, so common facets come up often, a muted facet never
    rolls, and a fresh 'less house' makes house roll less in the very next generation.

    `dists` is (genre_shares, era_shares) to roll from - normally the candidate pool's own mix (see
    pool_facets), so the theme is something this card can actually serve. Falls back to the whole
    library's distribution.

    `avoid` ({"genres": {...}, "eras": {...}}) holds facets a SIBLING card has already taken this
    render. A row of four cards all rolling the same theme is the common case for a focused library
    - the weighting that makes trance likely for one card makes it likely for all of them - and it
    wastes the row: which card you reach for is only signal if the cards differ. Avoidance is a
    preference, not a rule: if it would leave nothing to roll, the facet rolls unrestricted."""
    rng = random.Random(seed)
    avoid = avoid or {}
    weights = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    leans = transient.facet_leans(store, now) if now is not None else {}
    fgain = rec_params.get_param(store, "facet_gain")
    fmin = rec_params.get_param(store, "facet_mult_min")
    fmax = rec_params.get_param(store, "facet_mult_max")

    def pick(dist, prefix):
        items = [(k, share * weights.get(f"{prefix}:{k}", 1.0)
                     * transient.facet_multiplier(leans.get(f"{prefix}:{k}", 0.0), fgain, fmin, fmax))
                 for k, share in dist.items()]
        items = [(k, w) for k, w in items if w > 0]
        real = [(k, w) for k, w in items if dist.get(k, 0.0) >= _THEME_MIN_SHARE]
        items = real or items           # nothing substantial enough: take what there is
        taken = avoid.get("genres" if prefix == "genre" else "eras") or set()
        fresh = [(k, w) for k, w in items if k not in taken]
        items = fresh or items          # everything taken already: roll unrestricted rather than fail
        if not items:
            return None
        r = rng.random() * sum(w for _, w in items)
        acc = 0.0
        for k, w in items:
            acc += w
            if r <= acc:
                return k
        return items[-1][0]

    genre_dist, era_dist = dists or (None, None)
    genre = pick(genre_dist or taste_breadth(store)["families"], "genre")
    era = pick(era_dist or dict(era_distribution(store)), "era")
    # Fresh playlists are unowned proposals with no plays/recency, so data-dependent journeys
    # (rediscovery, deep dive, eras…) have no signal to order by. Keep them a straight shuffle.
    journey = "shuffle" if model == "fresh" else (pick(dict.fromkeys(journeys.JOURNEYS, 1.0), "journey") or "shuffle")
    facets = {}
    if genre:
        facets["genres"] = [genre]
    if era:
        facets["eras"] = [era]
    axis = {a: w for a, w in weights.items() if a.split(":", 1)[0] in ("genre", "era", "artist")}
    return {"model": model, "facets": facets, "params": {}, "journey": journey,
            "dj": {"stickiness": round(rng.random(), 2), "seed": rng.randint(0, 2**31 - 1)},
            "weights": axis}


def cluster_recipe(store, keep_keys, seed_labels=(), allow_families=(), journey="auto"):
    """Recipe + DJ-journey ordering for a saved Clusters mix (#15). model='cluster' gives the
    Generated playlist its own tunable type: the standard feedback panel applies, with a 'Made from'
    line built from the seeds you used, the genre families you restricted to (#29), and the genres /
    eras actually present; the chosen `journey` orders the tracks and makes the Flow lever real.

    `journey` is the user's DJ-Journey pick from the save bar; 'auto' (or anything unknown) ⇒ the
    'energy_arc' default. Unlike Home recipes this isn't theme-rolled. It just records what the
    cluster IS. Returns (recipe, ordered_keys); ordering is deterministic so a re-save lands the same."""
    journey = journey if journey in journeys.JOURNEYS else "energy_arc"
    keys = [k for k in dict.fromkeys(keep_keys) if k]
    dao = RecDao(store)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)
    lastp, plays = dao.track_last_played(keys), store.play_counts()
    meta = store.tracks_by_keys(keys)
    arc = arc_energy.arc_energies(keys, genres, dao.track_audio_features())   # real-audio energy (#37)
    fam_count, era_count = {}, {}
    for k in keys:
        if k in genres:
            fam = genre_map.family(genres[k])
            fam_count[fam] = fam_count.get(fam, 0) + 1
        if k in decades:
            era_count[decades[k]] = era_count.get(decades[k], 0) + 1
    facets = {}
    if seed_labels:
        facets["artists"] = list(dict.fromkeys(seed_labels))[:4]
    fams = list(dict.fromkeys(allow_families)) or \
        [f for f, _ in sorted(fam_count.items(), key=lambda x: -x[1])[:3]]
    if fams:
        facets["genres"] = fams
    eras = [d for d, _ in sorted(era_count.items())][:3]            # chronological decades present
    if eras:
        facets["eras"] = eras
    seed = zlib.crc32("|".join(keys).encode()) & 0x7FFFFFFF

    def feat(k):
        g = genres.get(k, "")
        return {"artist": (meta.get(k) or {}).get("artist", ""), "genre": g,
                "energy": arc.get(k, genre_map.energy(g)), "decade": decades.get(k),
                "plays": plays.get(k, 0), "recency": lastp.get(k, 0.0)}

    order = journeys.journey_order(keys, journey, seed, feat)
    recipe = {"model": "cluster", "facets": facets, "journey": journey,
              "params": {"seeds": list(seed_labels), "genre_whitelist": list(allow_families)},
              "dj": {"stickiness": 0.0, "seed": seed}, "weights": {}}
    return recipe, order


def theme_filter(store, items, facets, limit=None):
    """Focus a model's candidate items on the rolled theme: items whose genre family / decade match
    the recipe come first, the rest follow (so the card still fills if the theme is thin).

    Works for ForYouItems AND plain dicts (the Fresh card's proposals), and reads the genre/year an
    item CARRIES before falling back to the library. It used to do neither - it reached for `.key`
    with getattr, which is empty on a dict, and looked every genre up in the library, which knows
    nothing about an out-of-corpus track. So Fresh rolled a theme and then ignored it, every time."""
    fam_want, era_want = set(facets.get("genres", [])), set(facets.get("eras", []))
    if not fam_want and not era_want:
        return list(items)
    keys = [k for k in (_field(i, "key") or "" for i in items) if k]
    dao = RecDao(store)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)

    def facets_of(i):
        key = _field(i, "key") or ""
        genre = (_field(i, "genre") or "") or genres.get(key, "")
        year = _field(i, "year")
        decade = (str(int(year) // 10 * 10) if year else None) or decades.get(key)
        return (genre_map.family(genre) if genre else None), decade

    def matches(i):
        fam, decade = facets_of(i)
        return ((not fam_want) or (fam in fam_want)) and ((not era_want) or (decade in era_want))

    hit = [i for i in items if matches(i)]
    miss = [i for i in items if not matches(i)]
    out = hit + miss
    return out[:limit] if limit else out


def versioned_title(store, prefix) -> str:
    """'{prefix} #{n}', where n increments over existing playlists sharing that prefix, so every
    regenerate of a type that day gets its own version (e.g. 'Fresh songs - June 21 2026 #2')."""
    n = 1 + sum(1 for p in store.get_playlists() if p.title.startswith(prefix))
    return f"{prefix} #{n}"
