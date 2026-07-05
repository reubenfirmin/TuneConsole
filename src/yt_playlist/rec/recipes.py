"""Generated-playlist recipes: roll a Home theme, build a Clusters mix recipe (#15), and theme-filter
candidates onto the rolled theme. The recipe records what a generated playlist IS so it can be re-run."""
import random
import zlib

from yt_playlist.util import genre_map
from yt_playlist.rec import arc_energy, embed, journeys, rec_params, transient
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.rec.taste_analysis import era_distribution, taste_breadth


def roll_recipe(store, model, seed=None, now=None) -> dict:
    """Roll a per-playlist theme. Preference-weighted by your play distribution × permanent axis
    weights × the live transient facet leans, so common facets come up often, a muted facet never
    rolls, and a fresh 'less house' makes house roll less in the very next generation."""
    rng = random.Random(seed)
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
        if not items:
            return None
        r = rng.random() * sum(w for _, w in items)
        acc = 0.0
        for k, w in items:
            acc += w
            if r <= acc:
                return k
        return items[-1][0]

    genre = pick(taste_breadth(store)["families"], "genre")
    era = pick(dict(era_distribution(store)), "era")
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
    the recipe come first, the rest follow (so the card still fills if the theme is thin). Items are
    ForYouItems (use .key). A no-op for un-keyed/un-tagged candidates (e.g. fresh songs)."""
    fam_want, era_want = set(facets.get("genres", [])), set(facets.get("eras", []))
    if not fam_want and not era_want:
        return list(items)
    keys = [i.key for i in items if getattr(i, "key", "")]
    dao = RecDao(store)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)

    def matches(i):
        fam = genre_map.family(genres[i.key]) if getattr(i, "key", "") in genres else None
        g_ok = (not fam_want) or (fam in fam_want)
        e_ok = (not era_want) or (decades.get(getattr(i, "key", "")) in era_want)
        return g_ok and e_ok

    hit = [i for i in items if matches(i)]
    miss = [i for i in items if not matches(i)]
    out = hit + miss
    return out[:limit] if limit else out


def versioned_title(store, prefix) -> str:
    """'{prefix} #{n}', where n increments over existing playlists sharing that prefix, so every
    regenerate of a type that day gets its own version (e.g. 'Fresh songs - June 21 2026 #2')."""
    n = 1 + sum(1 for p in store.get_playlists() if p.title.startswith(prefix))
    return f"{prefix} #{n}"
