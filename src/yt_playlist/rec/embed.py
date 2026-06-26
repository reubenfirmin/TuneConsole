"""Taste-embedding model: PPMI co-occurrence + truncated SVD over the user's own curation.

Builds a dense vector per track from how tracks co-occur across the user's playlists, albums,
and listening sessions, a latent model of *their* taste, not the crowd's. Neighbours in this
space capture second-order similarity (tracks that never share a playlist but both sit near a
third), which plain co-occurrence cannot. CPU-only, no GPU, no external models.
"""
import json

import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.rec import rec_params
from yt_playlist.rec.rec_dao import RecDao

# Default embedding dimensionality. 48 is the recall@k-tuned default for manual builds; Auto-tune
# (eval_recs) may override it via the `rec_dim` setting. Not user-exposed (see rec_params docstring).
DIM = 48
# A truncated SVD needs more tracks than output dims to be well-posed; require a small margin above
# `dim` so the randomised solver has rank to work with (below this we return an empty model).
_MIN_VOCAB_MARGIN = 5
# Randomised-SVD oversampling: sketch with dim+_SVD_OVERSAMPLE columns so the random projection
# captures the true top-`dim` spectrum with high probability (standard Halko/Tropp sketch margin).
_SVD_OVERSAMPLE = 10
# Guard added to an L2 norm before dividing, so a zero/degenerate vector normalises to ~0 instead of
# raising or producing NaNs. Tiny relative to any real unit vector, so it never perturbs results.
_NORM_EPS = 1e-9
# item2vec SGNS internals (see _item2vec): unigram-smoothing exponent for negative sampling, std of
# the Gaussian weight initialisation, and the SGD minibatch size (memory/throughput, not quality).
_NEG_SAMPLE_SMOOTH = 0.75
_INIT_SCALE = 0.01
_SGNS_BATCH = 2048


def _normalize(v):
    """L2-normalize a 1-D vector with the standard epsilon guard (a zero vector maps to ~0, never NaN).
    For 2-D row-normalization use np.linalg.norm(..., axis=1, keepdims=True) inline."""
    return v / (np.linalg.norm(v) + _NORM_EPS)


def build_vectors(store, dim=DIM):
    """Return (keys, V), L2-normalised per-track embeddings. Method ('svd' default, or 'item2vec')
    comes from the recall@k-tuned `rec_embed_method` setting."""
    baskets = store.rec_baskets()
    keys = sorted({k for b in baskets for k in b})
    if len(keys) < dim + _MIN_VOCAB_MARGIN:
        return [], np.zeros((0, dim), dtype=np.float32)
    if (store.get_setting("rec_embed_method") or "svd") == "item2vec":
        return _item2vec(baskets, keys, dim)
    return _svd(baskets, keys, dim)


def _svd(baskets, keys, dim):
    """PPMI co-occurrence + randomised truncated SVD (numpy only)."""
    n = len(keys)
    idx = {k: i for i, k in enumerate(keys)}
    # Symmetric co-occurrence matrix C. Each basket contributes Newman's 1/(size-1) per member-pair
    # (Newman 2001): a track in a 50-item playlist shares weaker evidence per neighbour than one in a
    # duet, so large baskets don't dominate.
    C = np.zeros((n, n), dtype=np.float64)
    for b in baskets:
        ii = [idx[k] for k in b]
        w = 1.0 / (len(ii) - 1)
        for a in range(len(ii)):
            ia = ii[a]
            for c in range(a + 1, len(ii)):
                ic = ii[c]
                C[ia, ic] += w
                C[ic, ia] += w
    tot = C.sum()
    if tot <= 0:
        return [], np.zeros((0, dim), dtype=np.float32)
    # Positive PMI: log( P(i,j) / [P(i)P(j)] ), floored at 0. P is the joint (C/tot); `exp` is the
    # independence expectation (outer product of marginals). PPMI keeps only above-chance co-occurrence
    # (negative PMI is unreliable on sparse counts), giving the SVD a denser, better-conditioned input.
    row = C.sum(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        P = C / tot
        exp = np.outer(row, row) / (tot * tot)
        ppmi = np.maximum(np.log(np.where((P > 0) & (exp > 0), P / np.where(exp > 0, exp, 1.0), 1.0)), 0.0)
    # Randomised truncated SVD (Halko 2011): project PPMI through a Gaussian sketch of dim+oversample
    # columns, orthonormalise (QR), then do an exact small SVD in that subspace. O(n^2·dim) and numpy-
    # only, vs a full dense SVD. Fixed seed → identical vectors across rebuilds when the data is unchanged.
    rng = np.random.default_rng(0)
    om = rng.standard_normal((n, dim + _SVD_OVERSAMPLE))
    Q, _ = np.linalg.qr(ppmi @ om)
    Ub, S, _ = np.linalg.svd(Q.T @ ppmi, full_matrices=False)
    V = (Q @ Ub)[:, :dim] * np.sqrt(S[:dim])           # scale components by sqrt(singular value)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + _NORM_EPS
    return keys, V.astype(np.float32)


def _sigmoid(x):
    # Clip the logit to [-30, 30] before exp: exp(30) ~ 1e13 is already saturated, so this caps the
    # output at ~0/1 without changing it meaningfully while preventing exp overflow warnings/inf.
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


# item2vec SGNS hyperparameters. These are the standard word2vec/item2vec defaults (Mikolov 2013,
# Barkan & Koenigstein 2016), kept fixed because Auto-tune selects between SVD and item2vec by
# recall@k rather than tuning the optimiser: epochs over the pair stream, negatives per positive,
# and SGD learning rate. seed fixes initialisation for reproducible vectors across rebuilds.
def _item2vec(baskets, keys, dim, epochs=5, neg=5, lr=0.025, seed=0):
    """item2vec: skip-gram with negative sampling over baskets-as-sentences (numpy SGNS).

    Each basket is an unordered 'sentence', so every co-member is a context. Slightly better than
    SVD on the long tail per the literature, gated by recall@k in autotune, never forced.
    """
    rng = np.random.default_rng(seed)
    n = len(keys)
    idx = {k: i for i, k in enumerate(keys)}
    pairs = []
    for b in baskets:
        ii = [idx[k] for k in b]
        for a in range(len(ii)):
            for c in range(len(ii)):
                if a != c:
                    pairs.append((ii[a], ii[c]))
    if not pairs:
        return keys, np.zeros((n, dim), dtype=np.float32)
    pairs = np.asarray(pairs, dtype=np.int64)
    # Negative-sampling distribution, smoothed by the ^0.75 exponent from word2vec (Mikolov 2013):
    # flattens the unigram frequencies so popular tracks are still over-sampled as negatives but don't
    # swamp the long tail. +1.0 keeps every track sampleable.
    counts = np.bincount(pairs[:, 1], minlength=n).astype(np.float64) + 1.0
    pdist = counts ** _NEG_SAMPLE_SMOOTH
    pdist /= pdist.sum()
    # Small random init (standard SGNS): break symmetry while keeping initial dot-products near 0 so
    # early sigmoids sit in their linear region. Batch the pair stream for vectorised SGD updates.
    W = rng.standard_normal((n, dim)) * _INIT_SCALE
    Cm = rng.standard_normal((n, dim)) * _INIT_SCALE
    bs = _SGNS_BATCH
    for _ in range(epochs):
        perm = rng.permutation(len(pairs))
        for s in range(0, len(pairs), bs):
            bp = pairs[perm[s:s + bs]]
            ci, cj = bp[:, 0], bp[:, 1]
            m = len(bp)
            negs = rng.choice(n, size=(m, neg), p=pdist)
            wi = W[ci]                                   # (m, dim)
            gp = (_sigmoid((wi * Cm[cj]).sum(1)) - 1.0)[:, None]          # positive grad
            gn = _sigmoid((wi[:, None, :] * Cm[negs]).sum(2))[:, :, None]  # (m, neg, 1)
            gW = gp * Cm[cj] + (gn * Cm[negs]).sum(1)
            np.add.at(W, ci, -lr * gW)
            np.add.at(Cm, cj, -lr * (gp * wi))
            np.add.at(Cm, negs.ravel(), (-lr * gn * wi[:, None, :]).reshape(-1, dim))
    V = W / (np.linalg.norm(W, axis=1, keepdims=True) + _NORM_EPS)
    return keys, V.astype(np.float32)


def build_and_store(store, dim=None) -> int:
    """Build embeddings and persist them. Returns the number of tracks embedded.

    dim defaults to the recall@k-tuned `rec_dim` setting (or DIM if unset/untuned)."""
    if dim is None:
        dim = int(store.get_setting("rec_dim") or DIM)
    keys, V = build_vectors(store, dim)
    store.replace_rec_vectors([(k, V[i].tobytes()) for i, k in enumerate(keys)])
    return len(keys)


def _load_vector_rows(rows):
    """Unpack persisted (key, float32-bytes) rows into (keys, V, idx); ([], None, {}) when empty.
    Shared by the collaborative, content, and discovered-content vector loaders."""
    if not rows:
        return [], None, {}
    keys = [k for k, _ in rows]
    V = np.stack([np.frombuffer(v, dtype=np.float32) for _, v in rows])
    return keys, V, {k: i for i, k in enumerate(keys)}


def load_vectors(store):
    """Return (keys, V, idx) from persisted vectors, or ([], None, {}) if none built yet."""
    return _load_vector_rows(store.get_rec_vectors())


# --- content (genre/era) vectors: a "what a track IS" space, parallel to the collaborative one.
# Blended into cluster_expand so a seed reaches its own genre even when it never co-occurs there. ---
def content_features(content):
    """Build the content-feature index from {key: (genre, year4)}.

    Returns (feats, rows): feats maps a feature token -> column index; rows is a list of
    (key, [active column indices]). Tokens: fam:<family>, sub:<subgenre>, dec:<decade>. A track
    with a genre always has at least its family column, so it is never dropped for lack of a year.
    """
    feats: dict = {}

    def col(tok):
        return feats.setdefault(tok, len(feats))

    rows = []
    for k, (genre, year) in content.items():
        if not genre:
            continue
        active = [col(f"fam:{genre_map.family(genre)}")]
        sub = genre_map.subgenre(genre)
        if sub:
            active.append(col(f"sub:{sub}"))
        if year and str(year)[:4].isdigit():
            active.append(col(f"dec:{int(str(year)[:4]) // 10 * 10}"))
        rows.append((k, active))
    return feats, rows


# Continuous audio features that form the "sounds-like" block (z-scored across the corpus). Captures
# tempo, intensity, mood and production texture, the signal a genre tag can't express.
CONTINUOUS_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                    "mood_acoustic", "instrumental", "loudness", "dynamic_complexity")
AUDIO_DIM_W = 0.5      # per-audio-dim weight (z-score units) relative to a genre one-hot (1.0): audio
                       # refines WITHIN a genre without overwhelming it. The cluster_content_weight
                       # knob then sets how much this whole content space counts vs collaborative.


def build_content_model(content, audio):
    """The shared content space: a feature index + per-feature z-score stats, derived from the library.
    Persisted so that OUT-OF-CORPUS tracks (Phase 2) encode into the SAME space and their cosine to
    library vectors is meaningful. Returns {'cat': {token: col}, 'ncat': int, 'cont': [[feat,mu,sd]]}.

      • categorical one-hots: genre family, sub-genre, decade, musical key & scale;
      • continuous "sounds-like" features: bpm / energy / danceability / 4 moods / instrumental /
        loudness / dynamic-complexity, z-scored across the tracks that have them (zero-variance skipped).
    """
    cat, _ = content_features(content)               # genre/decade tokens → col
    cat = dict(cat)

    def cat_col(tok):
        return cat.setdefault(tok, len(cat))

    for d in audio.values():
        if d.get("music_scale"):
            cat_col(f"scale:{str(d['music_scale']).lower()}")
        if d.get("music_key"):
            cat_col(f"key:{d['music_key']}")
    cont = []
    for f in CONTINUOUS_AUDIO:
        vals = [float(d[f]) for d in audio.values() if d.get(f) is not None]
        if len(vals) >= 2:
            mu = sum(vals) / len(vals)
            sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
            if sd > 0:
                cont.append([f, mu, sd])
    return {"cat": cat, "ncat": len(cat), "cont": cont}


def encode_content(model, genre, year, audio):
    """Encode one (genre, year, audio dict) into `model`'s space → L2-normalized float32 vector, or
    None when the item has no feature the model knows. Tokens absent from the model contribute 0, so a
    track degrades gracefully to whatever shared signal it has."""
    cat, ncat, cont = model["cat"], model["ncat"], model["cont"]
    vec = np.zeros(ncat + len(cont), dtype=np.float32)
    toks = []
    if genre:
        toks.append(f"fam:{genre_map.family(genre)}")
        sub = genre_map.subgenre(genre)
        if sub:
            toks.append(f"sub:{sub}")
    if year and str(year)[:4].isdigit():
        toks.append(f"dec:{int(str(year)[:4]) // 10 * 10}")
    a = audio or {}
    if a.get("music_scale"):
        toks.append(f"scale:{str(a['music_scale']).lower()}")
    if a.get("music_key"):
        toks.append(f"key:{a['music_key']}")
    for t in toks:
        c = cat.get(t)
        if c is not None:
            vec[int(c)] = 1.0
    for j, (f, mu, sd) in enumerate(cont):
        val = a.get(f)
        if val is not None and sd:
            vec[ncat + j] = AUDIO_DIM_W * (float(val) - mu) / sd
    n = float(np.linalg.norm(vec))
    return (vec / n).astype(np.float32) if n > 0 else None


def _build_library_content(store):
    """Return (keys, V, model) for the library's content vectors, all in one freshly-built space."""
    dao = RecDao(store)
    content, audio = dao.track_content(), dao.track_audio_features()
    model = build_content_model(content, audio)
    keys, vecs = [], []
    for k in sorted(set(content) | set(audio)):
        genre, year = content.get(k, (None, None))
        v = encode_content(model, genre, year, audio.get(k))
        if v is not None:
            keys.append(k)
            vecs.append(v)
    V = np.stack(vecs).astype(np.float32) if keys else np.zeros((0, 0), dtype=np.float32)
    return keys, V, model


def build_content_vectors(store, dim=None):  # dim kept for signature symmetry; ignored
    """Return (keys, V): L2-normalized library content vectors (genre/era + audio sounds-like)."""
    keys, V, _ = _build_library_content(store)
    return keys, V


def build_content_and_store(store) -> int:
    """Build library content vectors + the shared model, persist both, and re-encode the out-of-corpus
    pool into the (just-rebuilt) space so the two stay comparable. Returns the library count."""
    keys, V, model = _build_library_content(store)
    store.replace_rec_content_vectors([(k, V[i].tobytes()) for i, k in enumerate(keys)])
    store.set_setting("rec_content_model", json.dumps(model))
    build_discovered_content_vectors(store, model)
    return len(keys)


def build_discovered_content_vectors(store, model=None) -> int:
    """Encode the out-of-corpus discovered-track pool into the shared content space (Phase 2). Uses the
    persisted model unless one is passed. Returns how many tracks got a vector."""
    if model is None:
        raw = store.get_setting("rec_content_model")
        if not raw:
            return 0
        model = json.loads(raw)
    out = []
    for r in store.get_discovered_tracks():
        v = encode_content(model, r.get("genre"), r.get("year"), r.get("audio"))
        if v is not None:
            out.append((r["identity_key"], v.tobytes()))
    store.replace_rec_discovered_content_vectors(out)
    return len(out)


def load_content_vectors(store):
    """Return (keys, V, idx) from persisted content vectors, or ([], None, {}) if none built."""
    return _load_vector_rows(store.get_rec_content_vectors())


def load_discovered_content_vectors(store):
    """Return (keys, V, idx) of out-of-corpus content vectors, or ([], None, {}) if none built."""
    return _load_vector_rows(store.get_rec_discovered_content_vectors())


def maybe_rebuild_content_vectors(store, step=0.05) -> bool:
    """Rebuild content vectors when genre coverage crosses into a higher `step` bucket (and on the
    first call). Coverage = (distinct tracks with a genre) / (distinct tracks). Bounds rebuilds to
    ~1/step over the library's enrichment lifetime while keeping the content space current as
    enrichment fills in. Returns True iff it rebuilt."""
    dao = RecDao(store)
    total = len(dao.library_keys())
    if total == 0:
        return False
    coverage = len(dao.track_content()) / total
    bucket = int(coverage // step)
    try:
        prev = int(store.get_setting("rec_content_cov_bucket", "-1"))
    except (TypeError, ValueError):
        prev = -1
    # The bucket gate bounds full rebuilds, but it must NOT short-circuit while the model itself is
    # missing: a build under older code persisted the vectors + bucket but not rec_content_model, which
    # left the gate permanently shut (bucket never re-crosses once coverage plateaus) and the discovered
    # pool unencodable forever, so "reach for new music" surfaced nothing (#48). A missing model always
    # forces a rebuild, which re-persists it and re-encodes the out-of-corpus pool in the same space.
    if bucket <= prev and store.get_setting("rec_content_model"):
        return False
    build_content_and_store(store)
    store.set_setting("rec_content_cov_bucket", str(bucket))
    return True


def _rank(keys, V, target, exclude, topn):
    sims = V @ target
    out = []
    for j in np.argsort(-sims):
        k = keys[j]
        if k in exclude:
            continue
        out.append((k, float(sims[j])))
        if len(out) >= topn:
            break
    return out


def neighbors(store, key, topn=12, exclude=None):
    """Tracks most similar to one seed track in taste space."""
    keys, V, idx = load_vectors(store)
    if V is None or key not in idx:
        return []
    return _rank(keys, V, V[idx[key]], (exclude or set()) | {key}, topn)


def neighbors_for_unmodeled(store, key, topn=12):
    """Neighbours for a seed track that has no vector of its own: it's brand new, or quarantined out
    of the embedding (an unplayed generated track). Query with a proxy: the centroid of the seed
    artist's tracks that ARE modeled. Lets 'songs like this' work for such tracks without putting the
    track itself into the model. Empty if the artist has nothing modeled."""
    keys, V, idx = load_vectors(store)
    if V is None or key in idx:
        return []
    artist = key.rsplit("|", 1)[-1]                      # identity_key = "title|artist" (normalized)
    rows = [idx[k] for k in idx if k != key and k.rsplit("|", 1)[-1] == artist]
    if not rows:
        return []
    proxy = V[rows].mean(0)
    n = np.linalg.norm(proxy)
    return _rank(keys, V, proxy / n, {key}, topn) if n > 0 else []


def _centroid(V, idx, seed_groups):
    target = np.zeros(V.shape[1], dtype=np.float64)
    for ks, w in seed_groups:
        si = [idx[k] for k in ks if k in idx]
        if si:
            c = V[si].mean(0)
            n = np.linalg.norm(c)
            if n > 0:
                target += w * (c / n)
    n = np.linalg.norm(target)
    return target / n if n > 0 else None


def sims_for(store, seed_groups, keys):
    """Cosine similarity of each given key to the weighted taste centroid (for Tier-2 re-ranking).
    Returns {key: sim}; empty if no vectors. Spec §8 'refined by Tier-2'."""
    allkeys, V, idx = load_vectors(store)
    if V is None:
        return {}
    target = _centroid(V, idx, seed_groups)
    if target is None:
        return {}
    return {k: float(V[idx[k]] @ target) for k in keys if k in idx}


def blended_neighbors(store, seed_groups, topn=12, exclude=None):
    """Rank by a weighted blend of several seed centroids.

    seed_groups = [(keys, weight), ...]: e.g. slow all-time taste at 0.6 + fast recent-mood at
    0.4. Each group's centroid is normalised before weighting, so a small recent set still counts.
    """
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    target = np.zeros(V.shape[1], dtype=np.float64)
    excl = set(exclude or set())
    for ks, w in seed_groups:
        si = [idx[k] for k in ks if k in idx]
        excl |= set(ks)
        if si:
            c = V[si].mean(0)
            n = np.linalg.norm(c)
            if n > 0:
                target += w * (c / n)
    if not np.any(target):
        return []
    return _rank(keys, V, _normalize(target), excl, topn)


CLUSTER_BETA = 0.6   # how hard a pruned ("negative model") track pushes a branch's ring away
SEED_FANOUT = 0.5    # for a MULTI-seed node, how much the ring is drawn to the NEAREST single seed
                     # vs the averaged centroid. >0 stops a minority seed (e.g. one psytrance pick
                     # among brit-rock) being averaged away. It reaches its own genre too. Self-
                     # adapting: when seeds are coherent (a focused path) max≈centroid, so it's a no-op.


def _branch_scores(pos_keys, neg_keys, beta, allkeys, M, index):
    """Per-key {key: pos_affinity - beta·cos(centroid_neg)} over one vector space, or None if no seed
    is present. pos_affinity blends proximity to the seeds' CENTROID with proximity to the NEAREST
    single seed (SEED_FANOUT), so a multi-seed node reaches each seed's neighbourhood instead of only
    the average (a minority seed isn't averaged away). Shared by both the collaborative and content
    spaces in cluster_expand."""
    pi = [index[k] for k in pos_keys if k in index]
    if not pi:
        return None
    pos = _normalize(M[pi].mean(0))
    s = M @ pos
    if len(pi) > 1 and SEED_FANOUT > 0.0:            # fan out to the nearest single seed (rows are unit)
        nearest = np.max(M @ M[pi].T, axis=1)         # max cos to any one seed, per candidate
        s = (1.0 - SEED_FANOUT) * s + SEED_FANOUT * nearest
    ni = [index[k] for k in neg_keys if k in index]
    if ni:
        neg = _normalize(M[ni].mean(0))
        s = s - beta * (M @ neg)
    return {allkeys[i]: float(s[i]) for i in range(len(allkeys))}


def _content_space(store, include_new):
    """Load the content (genre/era) vector space for cluster_expand. When include_new (Phase 2),
    widen it with the OUT-OF-CORPUS discovered-track pool, but only when they share the same model
    space (same column count). Returns (keys, V, idx)."""
    ckeys, CV, cidx = load_content_vectors(store)
    if include_new:
        dkeys, DV, _ = load_discovered_content_vectors(store)
        if DV is not None:
            if CV is None:
                ckeys, CV = list(dkeys), DV
            elif DV.shape[1] == CV.shape[1]:         # same model space - safe to stack
                ckeys = list(ckeys) + list(dkeys)
                CV = np.vstack([CV, DV])
        cidx = {k: i for i, k in enumerate(ckeys)}
    return ckeys, CV, cidx


def _blend_spaces(collab_s, content_s, w):
    """Combine the collaborative and content per-key scores: (1-w)·collab + w·content where a key has
    both, else whichever space holds it. So an untagged candidate (or w==0) scores on collaborative
    alone, and an out-of-corpus track with no collaborative vector scores on content alone."""
    blended = {}
    for k in collab_s.keys() | content_s.keys():
        has_c, has_t = k in collab_s, k in content_s
        if has_c and has_t:
            blended[k] = (1.0 - w) * collab_s[k] + w * content_s[k]
        elif has_c:
            blended[k] = collab_s[k]
        else:
            blended[k] = content_s[k]
    return blended


def cluster_expand(store, pos_keys, neg_keys=(), exclude=None, topn=12, beta=CLUSTER_BETA, allow=None,
                   include_new=False):
    """A Clusters-canvas ring: tracks nearest a node's PINNED-path centroid, tilted AWAY from the
    PRUNED set, scored as a blend of the collaborative (co-occurrence) embedding and the content
    (genre/era) vector: score = (1-w)·cos(collab) + w·cos(content), w = the cluster_content_weight
    knob. Each term is computed against the seeds' centroid in its own space; a candidate (or seed)
    missing one space is renormalized onto the other, so untagged tracks behave exactly as before
    and w=0 reproduces the pure-collaborative ring. pos/neg seeds and `exclude` are never returned.

    `allow`, when given, restricts candidates to a whitelist (#29 genre filter).
    `include_new` (Phase 2) widens the candidate pool with OUT-OF-CORPUS discovered tracks: they have
    no collaborative vector, so they're scored on the content term alone (in the same model space)."""
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    w = float(rec_params.get_param(store, "cluster_content_weight"))
    ckeys, CV, cidx = _content_space(store, include_new)

    collab_s = _branch_scores(pos_keys, neg_keys, beta, keys, V, idx) or {}
    content_s = ({} if (CV is None or w <= 0.0)
                 else (_branch_scores(pos_keys, neg_keys, beta, ckeys, CV, cidx) or {}))
    blended = _blend_spaces(collab_s, content_s, w)

    excl = set(exclude or ()) | set(pos_keys) | set(neg_keys)
    allow = None if allow is None else set(allow)
    out = []
    for k in sorted(blended, key=lambda k: -blended[k]):
        if k in excl or (allow is not None and k not in allow):
            continue
        out.append((k, blended[k]))
        if len(out) >= topn:
            break
    return out


def connection_geometry(store, key, path_keys, exclude=()):
    """Explain a Clusters edge in taste space: how close the child is to its pinned path, and, when
    there's no direct shared basket, the track that BRIDGES them.

    Returns {"score": cos(child, path_centroid) in [-1, 1], "bridge": key|None}. The bridge is the
    modelled track closest to BOTH ends, argmax over T of min(cos(child, T), cos(path_centroid, T)),
    excluding the child, the path, and `exclude`, i.e. the 'third track both sit near' that the
    second-order embedding is built to capture. Both fields are None until the model is built or if
    the child / path aren't modelled."""
    keys, V, idx = load_vectors(store)
    if V is None or key not in idx:
        return {"score": None, "bridge": None}
    pi = [idx[k] for k in path_keys if k in idx and k != key]
    if not pi:
        return {"score": None, "bridge": None}
    p = V[pi].mean(0)
    p = _normalize(p)
    c = V[idx[key]]
    score = float(c @ p)
    both = np.minimum(V @ c, V @ p)                  # close to child AND to the path
    excl = set(path_keys) | {key} | set(exclude)
    bridge = None
    for j in np.argsort(-both):
        if keys[j] not in excl:
            bridge = keys[j]
            break
    return {"score": score, "bridge": bridge}


def centroid_neighbors(store, seed_keys, topn=12, exclude=None):
    """Tracks most similar to the centroid of a set of seeds (e.g. a playlist's vibe)."""
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    si = [idx[k] for k in seed_keys if k in idx]
    if not si:
        return []
    centroid = V[si].mean(0)
    centroid = _normalize(centroid)
    return _rank(keys, V, centroid, set(exclude or set()) | set(seed_keys), topn)
