"""Taste-model scoring: build the per-playlist taste model and turn it into per-track scores,
re-weighted by genre/era/artist preferences, the breadth steer, and the transient mood/facet overlay.

Split out of the former monolithic recommend.py; recommend re-exports these for existing callers."""
import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.rec import embed, layers, rec_params, transient
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.rec.taste_analysis import taste_breadth


# Added to an L2 norm before dividing so a zero/degenerate vector normalises to ~0 rather than raising
# or producing NaNs. Tiny relative to any real unit vector, so it never perturbs a result.
_NORM_EPS = 1e-9


class PlaylistTaste:
    """Play-weighted per-playlist taste model: each playlist is one taste *context* (its embedding
    centroid), weighted by how much you actually listen to it. Scoring a candidate against this
    rewards fit to the contexts you play, so a low-play playlist (the 'vacation with Dad' problem)
    can't drag in off-taste recommendations, and distinct high-play contexts aren't blurred into one
    average. Catch-all playlists (too big to be a coherent context) are excluded.
    """

    def __init__(self, titles, centroids, weights, pids=()):
        self.titles = list(titles)               # playlist titles, one per context
        self.centroids = centroids               # (n, dim) L2-normalised rows, or empty
        self.weights = weights                   # (n,) sums to 1, or empty
        self.pids = list(pids)                   # playlist ids, aligned with titles (for the viz)

    def __bool__(self):
        return len(self.titles) > 0

    def score(self, vec, top=3):
        """(total, [(playlist_title, contribution), ...]) for a candidate taste vector."""
        if not self.titles:
            return 0.0, []
        v = vec / (np.linalg.norm(vec) + _NORM_EPS)
        contrib = self.weights * (self.centroids @ v)        # play-weighted cosine per context
        order = np.argsort(-contrib)[:top]
        because = [(self.titles[i], float(contrib[i])) for i in order if contrib[i] > 0]
        return float(contrib.sum()), because

    def score_all(self, V):
        """Per-context taste score for every row of V (N, dim) -> (N,). Vectorized."""
        if not self.titles or len(V) == 0:
            return np.zeros(len(V))
        Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + _NORM_EPS)
        return self.weights @ (self.centroids @ Vn.T)        # (P,)·(P,N) -> (N,)


def _playlist_centroids(store, M, idx) -> PlaylistTaste:
    """Per-playlist taste model over a vector space: each non-excluded playlist's normalized centroid in
    `M` (rows indexed by `idx` = {key: row}), play-weighted. Shared by playlist_taste (co-occurrence
    space) and content_taste (#38, content space). Generated + grab-bag catch-all playlists are skipped."""
    stats = store.get_playlist_listen_stats()                # {pid: (last_ts, listen_count)}
    excluded = RecDao(store).excluded_playlist_ids()         # generated playlists don't shape taste
    catchall = store.catchall_playlist_ids()                 # #38: large + genre-incoherent grab-bags only
    titles, cents, ws, pids = [], [], [], []
    for p in store.get_playlists():
        if p.id in excluded or p.id in catchall:             # skip generated + grab-bags (coherent large OK)
            continue
        rows = [idx[k] for k in store.get_playlist_track_keys(p.id) if k in idx]
        if not rows:
            continue
        c = M[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        titles.append(p.title)
        cents.append(c / n)
        ws.append(stats.get(p.id, (None, 0))[1] or 0)        # how much you listen to this playlist
        pids.append(p.id)
    if not titles:
        return PlaylistTaste([], np.zeros((0, 0)), np.zeros(0))
    w = np.asarray(ws, dtype=np.float64)
    w = w / w.sum() if w.sum() > 0 else np.full(len(titles), 1.0 / len(titles))   # uniform if no plays
    return PlaylistTaste(titles, np.asarray(cents), w, pids)


# How much of the permanent taste signal is "what you actually play" vs how you've filed tracks into
# playlists. 0.5 = co-equal. A #38 temporal_recall A/B found a played-tracks centroid ties-or-beats the
# playlist-curation taste (and that play-count/like/save weighting added nothing over flat membership),
# so behavior is folded in as a co-equal context. Re-tune once more history accrues (see the A/B ticket).
BEHAVIOR_TASTE_W = 0.5


def _behavior_centroid(store, M, idx):
    """Unit centroid of the tracks you've actually played, in vector space M (the behavior taste signal,
    #38). Flat membership: the A/B found play-count/like/save weighting added nothing over plain played-
    membership, so this stays simple. None when nothing you've played is modeled."""
    rows = [idx[k] for k in store.play_counts() if k in idx]
    if not rows:
        return None
    c = M[rows].mean(0)
    n = np.linalg.norm(c)
    return c / n if n > 0 else None


def playlist_taste(store) -> PlaylistTaste:
    """Per-playlist taste from the co-occurrence embedding, blended with a behavior centroid (#38): the
    tracks you actually play form one extra taste context weighted BEHAVIOR_TASTE_W, so taste leans on
    listening, not only playlist curation. Falls back to behavior-only when you have no shaping playlists,
    and to playlists-only when you've played nothing modeled."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return PlaylistTaste([], np.zeros((0, 0)), np.zeros(0))
    pt = _playlist_centroids(store, V, idx)
    bc = _behavior_centroid(store, V, idx)
    if bc is None:
        return pt
    if not pt:                                           # played tracks but no shaping playlists
        return PlaylistTaste(["Your listening"], bc[None, :], np.array([1.0]), [None])
    titles = list(pt.titles) + ["Your listening"]        # behavior as one more, co-equal context
    cents = np.vstack([pt.centroids, bc[None, :]])
    w = np.concatenate([pt.weights * (1.0 - BEHAVIOR_TASTE_W), [BEHAVIOR_TASTE_W]])
    return PlaylistTaste(titles, cents, w, list(pt.pids) + [None])


def content_taste(store) -> PlaylistTaste:
    """The content-space (genre/era + audio) sibling of playlist_taste (#38). Builds per-playlist taste
    centroids over the CONTENT vectors, so genre/era-described tracks that have no co-occurrence vector
    can still be ranked by how well they fit the contexts you actually play, no projection needed.
    Empty until content vectors are built."""
    ckeys, CV, cidx = embed.load_content_vectors(store)
    if CV is None:
        return PlaylistTaste([], np.zeros((0, 0)), np.zeros(0))
    return _playlist_centroids(store, CV, cidx)


def genre_adjusted_scores(scores, genre_of, gweights):
    """Re-weight per-track taste scores by the user's per-genre-family preferences.

    `gweights` maps genre family -> weight (1.0 = neutral, 0 = mute, >1 = favor). #86: weights
    apply to a rank/percentile base (see embed.percentile_scores), not a shift-by-min base, which
    BOUNDS how much the pool's worst score can distort the effect of a weight (it no longer warps
    it without limit; see the base's docstring for the residual). A muted family sinks to 0, a
    boosted one rises. Returns a new {key: score}; a pure no-op when all weights are neutral."""
    if not scores or not gweights or all(w == 1.0 for w in gweights.values()):
        return scores
    base = embed.percentile_scores(scores)
    return {k: base[k] * gweights.get(genre_of.get(k), 1.0) for k in scores}


def axis_adjusted_scores(scores, mult):
    """Re-weight taste scores by a precomputed per-key multiplier (generalizes genre weighting).

    #86: rank/percentile base (bounds pool influence; see embed.percentile_scores), not
    shift-by-min. No-op when `mult` is falsy or all-neutral. Returns a new {key: score}."""
    if not scores or not mult or all(m == 1.0 for m in mult.values()):
        return scores
    base = embed.percentile_scores(scores)
    return {k: base[k] * mult.get(k, 1.0) for k in scores}


# Breadth steering (#7): how far one full drag of the bias can push a single family's weight. The
# raw factor is share**(-bias*gain); these clamp it so a vanishingly rare family can't explode (nor a
# dominant one collapse) before it folds in with the manual genre weights.
BREADTH_FACTOR_MIN = 0.25


BREADTH_FACTOR_MAX = 4.0


def _breadth_factors(shares, bias, gain):
    """Per-genre-family multiplier that tilts how broad vs focused the feed's genre mix is (#7).

    This is the math kernel behind the interactive Breadth bar. It redistributes weight *across the
    families you already listen to* (it never invents genres you don't have):

      factor(fam) = clamp( r(fam) ** (-bias * gain) ),  where r(fam) = share(fam) * n_families

    `r` is a family's prominence relative to an even split (r=1 means exactly average). The exponent
    is what makes the dial work:
      - bias = 0  -> exponent 0 -> every factor is 1.0 (returned as {} so callers no-op): no change.
      - bias > 0  (eclectic) -> negative exponent -> rare families (r<1) lift above 1, dominant
        families (r>1) drop below 1. The genre distribution flattens toward uniform (higher entropy =
        more breadth), so your under-played families surface more.
      - bias < 0  (focused) -> the mirror image: dominant families are boosted, rare ones damped, so
        the feed concentrates on your core.

    Args:
        shares: {family: play_share} from `taste_breadth` (shares sum to ~1 over present families).
        bias:   the steer in [-1, +1] (0 neutral, + eclectic, - focused).
        gain:   sensitivity; one full drag roughly doubles a half/double-average family at gain=1.

    Returns {family: multiplier}, or {} when there's nothing to redistribute (neutral bias, or one
    family or fewer -> no spread to tilt). Callers treat a missing family as a neutral 1.0.
    """
    n = len(shares)
    if bias == 0.0 or n <= 1:
        return {}
    factors = {}
    for fam, share in shares.items():
        r = share * n                                  # prominence vs an even split (1.0 = average)
        raw = r ** (-bias * gain) if r > 0 else BREADTH_FACTOR_MAX   # r==0 -> maximally rare -> ceiling
        factors[fam] = max(BREADTH_FACTOR_MIN, min(BREADTH_FACTOR_MAX, raw))
    return factors


def _axis_mult(weights, kind, token, standing, leans, fparams):
    """One axis's multiplier: permanent weight x standing lean x transient facet multiplier, or a
    neutral 1.0 when the track has no value on that axis (token is None). Unifies the identical chain
    used for genre family, sub-genre, era and artist. fparams = (facet_gain, facet_mult_min, facet_mult_max)."""
    if token is None:
        return 1.0
    full = f"{kind}:{token}"
    transient_mult = transient.facet_multiplier(leans.get(full, 0.0), *fparams)
    return weights.get(token, 1.0) * standing.get(full, 1.0) * transient_mult


def _pop_band(popularity, threshold):
    """The popularity-axis token for a track (#43), or None (neutral) when it has no popularity or sits
    below the mainstream cut. One band today ('mainstream', what the 'too mainstream' dismiss steers);
    unknown popularity stays neutral, so the axis never excludes what we cannot classify."""
    if popularity is None:
        return None
    return "mainstream" if popularity >= threshold else None


def _axis_weights_for(store, keys, now=None):
    """{key: genre_w * era_w * artist_w * pop_w}, where each axis weight is permanent x standing lean x
    the transient facet multiplier (live 'more/less this facet'). None if every factor is neutral."""
    w = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    gw = {a[len("genre:"):]: v for a, v in w.items() if a.startswith("genre:")}
    ew = {a[len("era:"):]: v for a, v in w.items() if a.startswith("era:")}
    aw = {a[len("artist:"):]: v for a, v in w.items() if a.startswith("artist:")}
    pw = {a[len("pop:"):]: v for a, v in w.items() if a.startswith("pop:")}
    leans = transient.facet_leans(store, now) if now is not None else {}
    standing = store.get_leans()
    # Breadth steer (#7): a per-family tilt derived from your current genre spread. Only computed when
    # the bias is off-center (the taste_breadth query isn't worth running at the neutral default), and
    # it must keep the overlay alive below even when every genre/era/artist weight is neutral.
    bias = rec_params.get_param(store, "breadth_bias")
    bfac = _breadth_factors(taste_breadth(store)["families"], bias,
                            rec_params.get_param(store, "breadth_gain")) if bias else {}
    perm_neutral = all(v == 1.0 for v in
                       list(gw.values()) + list(ew.values()) + list(aw.values()) + list(pw.values()))
    if perm_neutral and not leans and not standing and not bfac:
        return None
    keys = list(keys)
    dao = RecDao(store)
    genres, decades, artists = dao.track_genres(keys), dao.track_decades(keys), dao.track_artists(keys)
    pops = dao.track_popularity(keys)
    pop_min = rec_params.get_param(store, "pop_mainstream_min")
    lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
    fparams = (rec_params.get_param(store, "facet_gain"),
               rec_params.get_param(store, "facet_mult_min"),
               rec_params.get_param(store, "facet_mult_max"))

    mult = {}
    for k in keys:
        fam = genre_map.family(genres[k]) if k in genres else None
        sub = genre_map.subgenre(genres[k]) if k in genres else None
        gm = _axis_mult(gw, "genre", fam, standing, leans, fparams) * bfac.get(fam, 1.0)   # breadth folds in
        if sub and sub != fam:
            gm *= _axis_mult(gw, "genre", sub, standing, leans, fparams)
        em = _axis_mult(ew, "era", decades.get(k), standing, leans, fparams)
        am = _axis_mult(aw, "artist", artists.get(k), standing, leans, fparams)
        pm = _axis_mult(pw, "pop", _pop_band(pops.get(k), pop_min), standing, leans, fparams)
        mult[k] = (max(lo, min(hi, gm)) * max(lo, min(hi, em))
                   * max(lo, min(hi, am)) * max(lo, min(hi, pm)))
    return mult


def _apply_axis_weights(store, sims, now=None):
    """Re-weight a {key: taste-score} map by permanent preferences × the live transient facet leans."""
    mult = _axis_weights_for(store, list(sims), now=now)
    return sims if mult is None else axis_adjusted_scores(sims, mult)


def discovery_facet_weight(store, family, now):
    """#18: the facet overlay for OUTWARD discovery (new artists/albums), at genre-family granularity.

    Returns a positive multiplier to scale a candidate's surfacing score by, or None to HARD-EXCLUDE
    it (when the family's permanent `genre:` weight is exactly 0: muting techno on the Taste tab
    yields zero techno discovery, stronger than the in-library de-rank). The multiplier is
    permanent_weight × standing_lean × a DAMPED transient multiplier, so 'mute' bends hard while
    'less house lately' only nudges. An untagged candidate (family falsy) is neutral (1.0) and never
    excluded. We don't banish what we can't classify."""
    if not family:
        return 1.0
    perm = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d")).get(f"genre:{family}", 1.0)
    if perm == 0:
        return None
    standing = store.get_lean(f"genre:{family}")
    lean = transient.facet_leans(store, now).get(f"genre:{family}", 0.0)
    tmult = transient.facet_multiplier(lean * rec_params.DISCOVERY_TRANSIENT_DAMP,
                                       rec_params.FACET_GAIN, rec_params.FACET_MULT_MIN,
                                       rec_params.FACET_MULT_MAX)
    return max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, perm * standing * tmult))


def genre_distance_fn(store, alpha=0.5):
    """A genre-distance function blending the static meta-genre map with this library's own
    co-occurrence: genres you repeatedly playlist together are pulled closer. alpha = static
    weight. Falls back to the static map for pairs you've never grouped. Spec §2.1/§5.3.
    """
    co = store.genre_cooccurrence()
    pairs, occ = co["pairs"], co["occ"]

    def dist(g1, g2):
        base = genre_map.distance(g1, g2)
        a, b = (g1, g2) if g1 <= g2 else (g2, g1)
        c = pairs.get((a, b), 0)
        if c == 0 or not occ.get(g1) or not occ.get(g2):
            return base
        jaccard = c / (occ[g1] + occ[g2] - c)
        return alpha * base + (1 - alpha) * (1 - jaccard)

    return dist


MOOD_ALPHA = 0.35   # how hard a mood event tilts the lanes, relative to the taste score


mood_tilt = transient.centroid_tilt   # back-compat: tests/callers use recommend.mood_tilt(store, now, V, idx)


def _audio_tilt_boost(store, now, idx, content_vecs=None):
    """Per-row cosine of each candidate's CONTENT vector to the recent-listening audio direction
    (#45), aligned to V rows via `idx` (key -> row). None when there is no audio tilt or no content
    vectors built. A candidate without a content vector contributes 0, so the term degrades gracefully
    as audio coverage rises rather than being all-or-nothing.

    `content_vecs` (a (keys, CV, cidx) triple) overrides the source: warm callers leave it None and get
    the library content vectors; the cold path passes the discovered-content vectors, whose keys are
    out-of-corpus and so absent from the library store."""
    atilt = transient.audio_centroid_tilt(store, now)
    if atilt is None:
        return None
    _keys, CV, cidx = embed.load_content_vectors(store) if content_vecs is None else content_vecs
    if CV is None:
        return None
    boost = np.zeros(len(idx))
    for k, r in idx.items():
        ci = cidx.get(k)
        if ci is not None:
            boost[r] = float(CV[ci] @ atilt)
    return boost


def _apply_mood(scores, store, now, V, idx, content_vecs=None):
    """Blend the transient tilts into per-track scores. #85: each tilt decays internally, per event, on
    its own wall clock (see transient.decay_weight); there is no separate external staleness relax here
    any more, so this just adds the (already-decayed) tilts straight in.

    #88: three tilts from the same recent stream (plays/likes/mood), each at its own timescale, blended
    alongside one another: the transient collaborative centroid tilt (co-listen space, days half-lives),
    the SESSION tilt (layers.session_tilt, same collaborative space, hours half-life: the current
    listening session's carry-over), and the audio centroid tilt (#45, the content space), so ranking
    can lean toward the SOUND (tempo/energy/mood) of what you have been playing, not only its
    genre/era/artist facets. They apply independently: a candidate missing one space simply skips that
    term, and the audio tilt can still fire for recent plays that have a content vector but no
    collaborative one.

    `content_vecs` selects the audio tilt's content-vector source: warm callers leave it None (library
    vectors); the cold path passes the discovered-content vectors so out-of-corpus tracks get the tilt."""
    tilt = transient.centroid_tilt(store, now, V, idx)
    if tilt is not None:
        Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + _NORM_EPS)
        scores = scores + MOOD_ALPHA * (Vn @ tilt)
    st = layers.session_tilt(store, now, V, idx)
    if st is not None:
        Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + _NORM_EPS)
        session_alpha = rec_params.get_param(store, "session_alpha")
        scores = scores + session_alpha * (Vn @ st)
    w = rec_params.get_param(store, "audio_transient_w")
    if w > 0:
        boost = _audio_tilt_boost(store, now, idx, content_vecs=content_vecs)
        if boost is not None:
            scores = scores + w * boost
    return scores
