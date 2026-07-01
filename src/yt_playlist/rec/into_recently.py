"""Pick the freshest, least-obvious artist or genre the user is into right now.

Candidates come from the transient model's facet leans. Each is scored by lean strength times a
novelty factor that suppresses the obvious: an artist who is already an all-time favorite is dropped
(you are not "newly into" them), and a genre that is a broad staple of the library is driven toward
zero, so the card surfaces a specific rising taste ('shoegaze') rather than a truism ('rock').
"""
from yt_playlist.rec import transient
from yt_playlist.util import genre_map

FAVORITE_TOP_N = 15        # all-time top artists treated as settled favorites (excluded)
GENRE_OBVIOUS_SHARE = 0.20  # a genre at or above this share of the library is fully "obvious"
PREWARM_POOL = 12          # how many top subjects RecWorker pre-fetches wiki cards for, so several
                           # are warm at once and the card can erode through them (see subjects_for_epoch)
SUBGENRE_TIEBREAK = 1.05    # nudge specific subgenres above their broad family
CLUSTER_DEPTH = 2          # how many rings the "explore in your catalog" CTA grows (kept tight)

# A fun, saturated colour per genre family, used to tint + glow the card heading (techno -> green,
# etc). Genre subjects map by family; artists (and unknown families) fall back to the accent.
_FAMILY_COLORS = {
    "techno": "#15e98c", "house": "#ff8a3d", "trance": "#7c6cff", "dnb": "#ff4f8b",
    "breakbeat": "#ffb01f", "garage-bass": "#3df0d0", "ambient": "#6cc6ff", "electro-synth": "#a596ff",
    "rock-classic": "#ff6b4a", "rock-indie": "#4fd6e0", "rock-post": "#9a8cff", "metal": "#c0c6d0",
    "punk": "#ff5470", "pop": "#ff7ad1", "hiphop": "#ffd23f", "soul-funk": "#ff9e3d",
    "jazz": "#5db4ff", "blues": "#4f9dff", "folk-country": "#d6a86a", "world-latin": "#ff6f3c",
    "classical": "#e8d9a0", "experimental": "#b06cff",
}
_DEFAULT_COLOR = "#a596ff"  # artists and unknown genres glow in the house accent


def _norm(s):
    return (s or "").strip().lower()


def _baseline_genre_shares(store) -> dict:
    """Library share per facet token (family and subgenre), from the owned-track genre distribution.
    corpus_distribution is keyed by raw genre; fold it into the same family/subgenre tokens the
    transient facets use so shares are directly comparable."""
    corpus = store.corpus_distribution("genre")
    total = sum(corpus.values()) or 1
    counts: dict = {}
    for g, c in corpus.items():
        fam = genre_map.family(g)
        if fam:
            counts[fam] = counts.get(fam, 0) + c
        sub = genre_map.subgenre(g)
        if sub and sub != fam:
            counts[sub] = counts.get(sub, 0) + c
    return {tok: c / total for tok, c in counts.items()}


def ranked_subjects(store, now) -> list:
    """All fresh, non-obvious subjects as {kind, subject, display}, strongest first (may be empty).

    The route walks this list and renders the first subject that actually resolves to a Wikipedia
    card, so a rotation landing on a subject with no good page never blanks the card."""
    leans = transient.facet_leans(store, now)
    favorites = {_norm(a["artist"]) for a in store.top_artists(limit=FAVORITE_TOP_N)}
    genre_base = _baseline_genre_shares(store)
    scored = []
    for facet, strength in leans.items():
        if strength <= 0:
            continue
        kind, _, name = facet.partition(":")
        if kind == "artist":
            if _norm(name) in favorites:
                continue
            score, display = strength, name
        elif kind == "genre":
            share = genre_base.get(name, 0.0)
            novelty = 1.0 - min(1.0, share / GENRE_OBVIOUS_SHARE)
            if novelty <= 0:
                continue
            score = strength * novelty
            if genre_map.subgenre(name) == name:   # token is a specific subgenre, not a family
                score *= SUBGENRE_TIEBREAK
            display = name.replace("-", " ")
        else:
            continue                                # era: and anything else: not a Wikipedia subject
        scored.append((score, kind, facet, display))
    scored.sort(key=lambda t: (-t[0], t[2]))        # strongest first, name as a stable tie-break
    return [{"kind": k, "subject": f, "display": d} for (_s, k, f, d) in scored]


def _is_warm(store, subj, now) -> bool:
    """True if this subject already has a usable (fresh, found, non-empty) Wikipedia card cached, so
    the route can render it without a live fetch. The RecWorker prewarm keeps the top of the pool warm."""
    row = store.wiki.get(subj["subject"])
    return bool(row and store.wiki.is_fresh(row, now) and row["found"] and row["extract"])


def subjects_for_epoch(store, now, epoch=0) -> list:
    """Ordered subject candidates for the route to try this epoch (first that resolves wins).

    The card must erode like every other Home card. The buggy old behaviour only reordered the top 3
    subjects, so when the strongest few didn't resolve to a Wikipedia page the walk collapsed onto the
    single subject that did (e.g. 'techno' forever). Instead: rotate by `epoch` through the subjects
    whose card is already WARM in the cache (one distinct subject per epoch), so the card cycles. Cold
    (un-cached / stale) subjects follow in rank order as a fallback for a cold start before the prewarm
    has run. Fresh negative-cache misses naturally fall in the tail and the route skips them."""
    warm, rest = [], []
    for subj in ranked_subjects(store, now):
        (warm if _is_warm(store, subj, now) else rest).append(subj)
    if warm:
        off = epoch % len(warm)
        warm = warm[off:] + warm[:off]
    return warm + rest


def prewarm_pool(store, now, fetch_fn=None, limit=PREWARM_POOL) -> int:
    """Pre-fetch and cache Wikipedia cards for the top `limit` fresh subjects.

    Run off the request path (by RecWorker, after a rebuild) so the Home 'into recently' card serves
    from a warm cache instead of blocking on a live Wikipedia fetch when the rotation lands on a new
    subject. Subjects already fresh in the cache are skipped; both hits and misses are written, so the
    negative cache keeps the route from re-walking dead subjects. Returns the number fetched."""
    if fetch_fn is None:
        from yt_playlist.providers import wikipedia
        fetch_fn = wikipedia.fetch_summary
    fetched = 0
    for subj in ranked_subjects(store, now)[:limit]:
        row = store.wiki.get(subj["subject"])
        if row is not None and store.wiki.is_fresh(row, now):
            continue
        payload = fetch_fn(subj["kind"], subj["display"])
        store.wiki.put(subj["subject"], subj["kind"], subj["display"], payload, now)
        fetched += 1
    return fetched


def subject_color(subj) -> str:
    """The heading tint/glow colour for a picked subject. Genres map by family; artists use accent."""
    if subj["kind"] != "genre":
        return _DEFAULT_COLOR
    token = subj["subject"].split(":", 1)[1]
    return _FAMILY_COLORS.get(token) or _FAMILY_COLORS.get(genre_map.family(token), _DEFAULT_COLOR)


def _genres_in_token(store, token) -> list:
    """Raw library genres that fold into a facet token (its family or its own subgenre name)."""
    return [g for g in store.corpus_distribution("genre")
            if genre_map.family(g) == token or genre_map.subgenre(g) == token]


def decorate(store, subj) -> dict:
    """Card extras for a picked subject: a representative local thumbnail, the heading colour, and the
    seed/genre used to deep-link the Clusters explorer. The thumbnail is a fallback for when Wikipedia
    has no image (the card always wants one); seed is the library item the explorer clusters around."""
    color = subject_color(subj)
    if subj["kind"] == "artist":
        return {"color": color, "thumbnail": store.artist_thumbnail(subj["display"]),
                "seed": subj["display"], "genre": None}
    token = subj["subject"].split(":", 1)[1]
    rep = store.genre_representative(_genres_in_token(store, token))
    return {"color": color, "thumbnail": rep["thumbnail"] if rep else None,
            "seed": rep["artist"] if rep else None, "genre": token}
