"""Taste-embedding model: PPMI co-occurrence + truncated SVD over the user's own curation.

Builds a dense vector per track from how tracks co-occur across the user's playlists, albums,
and listening sessions, a latent model of *their* taste, not the crowd's. Neighbours in this
space capture second-order similarity (tracks that never share a playlist but both sit near a
third), which plain co-occurrence cannot. CPU-only, no GPU, no external models.
"""
import hashlib
import itertools
import json
import math

import numpy as np
from scipy.sparse import csr_array

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec.pair_stream import PairStream
from yt_playlist.util import genre_map
from yt_playlist.rec import rec_params

# Default embedding dimensionality. Automatic model selection may override it through `rec_dim`.
# Not user-exposed (see rec_params docstring).
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
# Bound temporary basket incidence storage and keep cancellation responsive during accumulation.
_COOC_BASKET_BATCH = 256


def _normalize(v):
    """L2-normalize a 1-D vector with the standard epsilon guard (a zero vector maps to ~0, never NaN).
    For 2-D row-normalization use np.linalg.norm(..., axis=1, keepdims=True) inline."""
    return v / (np.linalg.norm(v) + _NORM_EPS)


# === Collaborative vectors: PPMI co-occurrence + truncated SVD / item2vec over the user's curation ===
def build_vectors(store, dim=DIM):
    """Return (keys, V), L2-normalised per-track embeddings. Method ('svd' default, or 'item2vec')
    comes from the recall@k-tuned `rec_embed_method` setting."""
    baskets = store.rec_query.rec_baskets()
    return build_vectors_from_baskets(
        baskets, dim=dim,
        method=store.settings.get_setting("rec_embed_method") or "svd",
        weights=_cooc_weights(store) if store.settings.get_setting("rec_cooc_weighting") == "1" else None,
    )


def build_vectors_from_baskets(baskets, dim=DIM, method="svd", weights=None):
    """Fit vectors from an explicit basket snapshot without touching live model state.

    The offline evaluator uses this seam to fit an as-of model.  Keeping the vectors in memory is
    important: a backtest must neither read the current persisted vectors nor replace the live model.
    """
    check_stopping()
    keys = sorted({k for b in baskets for k in b})
    if len(keys) < dim + _MIN_VOCAB_MARGIN:
        return [], np.zeros((0, dim), dtype=np.float32)
    if method == "item2vec":
        return _item2vec(baskets, keys, dim)
    return _svd(baskets, keys, dim, weights)


def _cooc_weights(store):
    """#38 §4c per-track co-occurrence gains from play frequency: g(k) = 1 + log1p(playcount), so a
    heavily-played track's pairings count for more than a one-off. A never-played track keeps gain 1.0
    (the binary baseline), so the weighting only ever amplifies existing evidence, never erases it.
    Recency weighting is a natural extension; playcount is the signal available without a history join.

    VERDICT (measured 2026-06-26 on the real library: 4303 vectors, ~5.7 days of retained history): this
    does NOT help, so `rec_cooc_weighting` ships OFF by default. The retired manual harness compared
    temporal_recall (predict-your-next-plays): binary membership beat or tied playcount-weighting at every
    holdout window tested (0.5d..3d; deltas -0.045 .. 0.000, weighting never ahead). In-sample recall@k
    slightly favored weighting (0.768 -> 0.777), but that is exactly the overfit-to-heavy-tracks artifact
    temporal_recall is there to expose. Only flip the setting on if a future re-run on more retained
    history reverses this using isolated candidate fitting, without mutating the serving model."""
    return {k: 1.0 + math.log1p(c) for k, c in store.rec_query.play_counts().items() if c}


def _cooc_matrix(baskets, keys, weights=None):
    """Symmetric co-occurrence matrix over `keys`. Each basket contributes Newman's 1/(size-1) per
    member-pair (Newman 2001): a track in a 50-item playlist shares weaker evidence per neighbour than
    one in a duet, so large baskets don't dominate. #38 §4c: when `weights` is given ({key: gain}), each
    pair is additionally scaled by gain_i * gain_j; weights=None (the default) is binary membership."""
    n = len(keys)
    idx = {k: i for i, k in enumerate(keys)}
    g = None if weights is None else np.array([float(weights.get(k, 1.0)) for k in keys])
    C = csr_array((n, n), dtype=np.float64)
    for start in range(0, len(baskets), _COOC_BASKET_BATCH):
        check_stopping()
        indices, indptr, gains = [], [0], []
        for b in baskets[start:start + _COOC_BASKET_BATCH]:
            check_stopping()
            ii = [idx[k] for k in dict.fromkeys(b)]
            if len(ii) < 2:
                continue
            indices.extend(ii)
            indptr.append(len(indices))
            gains.append(1.0 / (len(ii) - 1))
        if not gains:
            continue
        incidence = csr_array((np.ones(len(indices)), np.asarray(indices, dtype=np.int64),
                               np.asarray(indptr, dtype=np.int64)), shape=(len(gains), n))
        block = (incidence.T @ incidence.multiply(np.asarray(gains)[:, None])).tocsr()
        block.setdiag(0)
        block.eliminate_zeros()
        C = C + block
    check_stopping()
    if g is not None:
        C = C.multiply(g[:, None]).multiply(g[None, :]).tocsr()
        C.eliminate_zeros()
    return C


def _positive_pmi(C):
    """PPMI has no entries where co-occurrence is zero; never form the dense marginal product."""
    total = float(C.sum())
    if total <= 0:
        return csr_array(C.shape, dtype=np.float64)
    row = np.asarray(C.sum(axis=1)).ravel()
    entries = C.tocoo(copy=False)
    values = np.maximum(np.log((entries.data / row[entries.row])
                               * (total / row[entries.col])), 0.0)
    ppmi = csr_array((values, (entries.row, entries.col)), shape=C.shape)
    ppmi.eliminate_zeros()
    return ppmi


def _svd(baskets, keys, dim, weights=None):
    """Sparse PPMI + the existing randomised truncated SVD. #38 §4c: optional per-track
    co-occurrence weighting via `weights` (None = binary membership, the default)."""
    n = len(keys)
    ppmi = _positive_pmi(_cooc_matrix(baskets, keys, weights))
    if not ppmi.nnz:
        return [], np.zeros((0, dim), dtype=np.float32)
    # Randomised truncated SVD (Halko 2011): project PPMI through a Gaussian sketch of dim+oversample
    # columns, orthonormalise (QR), then do an exact small SVD in that subspace. Only n-by-sketch
    # working arrays are dense. Preserve the original sketch width for small libraries too.
    rng = np.random.default_rng(0)
    om = rng.standard_normal((n, dim + _SVD_OVERSAMPLE))
    check_stopping()
    Q, _ = np.linalg.qr(ppmi @ om)
    check_stopping()
    Ub, S, _ = np.linalg.svd((ppmi.T @ Q).T, full_matrices=False)
    check_stopping()
    V = (Q @ Ub)[:, :dim] * np.sqrt(S[:dim])           # project back into track space, scale by sqrt(singular value)
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
    pairs = PairStream(baskets, keys)
    if not pairs.count:
        return keys, np.zeros((n, dim), dtype=np.float32)
    # Negative-sampling distribution, smoothed by the ^0.75 exponent from word2vec (Mikolov 2013):
    # flattens the unigram frequencies so popular tracks are still over-sampled as negatives but don't
    # swamp the long tail. +1.0 keeps every track sampleable.
    pdist = (pairs.context_counts + 1.0) ** _NEG_SAMPLE_SMOOTH
    pdist /= pdist.sum()
    # Small random init (standard SGNS): break symmetry while keeping initial dot-products near 0 so
    # early sigmoids sit in their linear region. Batch the pair stream for vectorised SGD updates.
    W = rng.standard_normal((n, dim)) * _INIT_SCALE
    Cm = rng.standard_normal((n, dim)) * _INIT_SCALE
    bs = _SGNS_BATCH
    for _ in range(epochs):
        for ci, cj in pairs.batches(rng, bs):
            check_stopping()
            m = len(ci)
            negs = rng.choice(n, size=(m, neg), p=pdist)
            wi = W[ci]                                   # (m, dim) center vectors for this batch
            gp = (_sigmoid((wi * Cm[cj]).sum(1)) - 1.0)[:, None]          # positive grad: (m,1)
            # negative grad: (m,neg,dim) elementwise -> sum over dim -> (m,neg) sigmoid -> (m,neg,1)
            gn = _sigmoid((wi[:, None, :] * Cm[negs]).sum(2))[:, :, None]
            gW = gp * Cm[cj] + (gn * Cm[negs]).sum(1)    # (m,dim): toward context, away from negatives
            np.add.at(W, ci, -lr * gW)
            np.add.at(Cm, cj, -lr * (gp * wi))
            np.add.at(Cm, negs.ravel(), (-lr * gn * wi[:, None, :]).reshape(-1, dim))
    V = W / (np.linalg.norm(W, axis=1, keepdims=True) + _NORM_EPS)
    return keys, V.astype(np.float32)


def _load_vector_rows(rows):
    """Unpack persisted (key, float32-bytes) rows into (keys, V, idx); ([], None, {}) when empty.
    Shared by the collaborative, content, and discovered-content vector loaders."""
    if not rows:
        return [], None, {}
    check_stopping()
    keys = [k for k, _ in rows]
    vectors = []
    for i, (_, vector) in enumerate(rows):
        if i % 256 == 0:
            check_stopping()
        vectors.append(np.frombuffer(vector, dtype=np.float32))
    V = np.stack(vectors)
    check_stopping()
    return keys, V, {k: i for i, k in enumerate(keys)}


def load_vectors(store):
    """Return (keys, V, idx) from persisted vectors, or ([], None, {}) if none built yet."""
    return _load_vector_rows(store.rec_model.get_rec_vectors())


# === Content vectors: a "what a track IS" space (genre/era + audio), parallel to the collaborative one.
# Blended into cluster_expand so a seed reaches its own genre even when it never co-occurs there. ===
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
        active = []
        if genre:
            active.append(col(f"fam:{genre_map.family(genre)}"))
            sub = genre_map.subgenre(genre)
            if sub:
                active.append(col(f"sub:{sub}"))
        if year and str(year)[:4].isdigit():
            active.append(col(f"dec:{int(str(year)[:4]) // 10 * 10}"))
        if active:
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
            mu = float(np.mean(vals))                    # cast off np.float64 so the model stays JSON-serializable
            sd = float(np.std(vals))                     # population std (ddof=0), matching the old manual form
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


def _content_inputs(store):
    dao = store.rec_query
    return sorted(dao.library_keys()), dao.track_content(), dao.track_audio_features()


def _content_input_fingerprint(inputs):
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def _build_library_content(store, *, inputs=None):
    """Return (keys, V, model) for the library's content vectors, all in one freshly-built space."""
    _, content, audio = inputs if inputs is not None else _content_inputs(store)
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


def content_model_fingerprint(model) -> str:
    """Identify the content SPACE, so a vector built in one is never compared against another.

    `build_content_model` assigns categorical columns by insertion order, so a single new sub-genre
    or musical key both grows the dimension and can shift what a column *means*. Anything holding a
    persisted vector (taste-mode centroids) must be able to notice that its space is gone: a bare
    dimension check would miss a same-size reordering and silently produce meaningless cosines.

    Keyed on the token→column mapping and the continuous feature order, NOT on the z-score mu/sd,
    which drift on every rebuild. Treating that drift as a new space would retire every taste mode
    each pass; the geometry it induces is close enough for centroid matching to remain meaningful.
    """
    payload = json.dumps({
        "catalog_identity_version": 2,
        "cat": sorted(model.get("cat", {}).items(), key=lambda kv: (kv[1], kv[0])),
        "cont": [c[0] for c in model.get("cont", [])],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def content_space_id(store, *, snapshot=None) -> str:
    """Fingerprint of the currently-persisted content model, or "" when none is built yet."""
    raw = snapshot["model"] if snapshot is not None else store.settings.get_setting("rec_content_model")
    return content_model_fingerprint(json.loads(raw)) if raw else ""


def build_content_vectors(store, dim=None):  # dim kept for signature symmetry; ignored
    """Return (keys, V): L2-normalized library content vectors (genre/era + audio sounds-like)."""
    keys, V, _ = _build_library_content(store)
    return keys, V


def _encode_discovered(rows, model):
    out = []
    for r in rows:
        v = encode_content(model, r.get("genre"), r.get("year"), r.get("audio"))
        if v is not None:
            out.append((r["identity_key"], v.tobytes()))
    return out


def build_discovered_content_vectors(store) -> int:
    """Encode the out-of-corpus discovered-track pool into the shared content space (Phase 2). Uses the
    persisted model. A competing model publication forces a fresh encoding, never a mixed pool."""
    for _ in range(3):
        snapshot = store.rec_model.content_generation()
        if not snapshot["model"]:
            return 0
        out = _encode_discovered(store.discovery.get_discovered_tracks(), json.loads(snapshot["model"]))
        if store.rec_model.publish_content_generation(snapshot["library"], out, snapshot["model"],
                snapshot["revision"] or "", snapshot["inputs"], snapshot["generation"]):
            return len(out)
    raise RuntimeError("Content generation changed during discovery encoding; retry the refresh")


def load_content_vectors(store, *, snapshot=None):
    """Return (keys, V, idx) from persisted content vectors, or ([], None, {}) if none built."""
    return _load_vector_rows(snapshot["library"] if snapshot is not None else store.rec_model.get_rec_content_vectors())


def load_discovered_content_vectors(store, *, snapshot=None):
    """Return (keys, V, idx) of out-of-corpus content vectors, or ([], None, {}) if none built."""
    return _load_vector_rows(snapshot["discovered"] if snapshot is not None else store.rec_model.get_rec_discovered_content_vectors())


# === Ranking & neighbours: query the built vector space (taste neighbours, centroids, bridges) ===
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


def neighbors_blended(store, key, topn=12):
    """#105 Neighbours from BOTH spaces, so "songs like this" can reach tracks you do not own yet.

    The collaborative space is built from playlist co-occurrence, so by construction it can only ever
    contain tracks that are already in your playlists: the discovered pool is invisible to it, and
    "songs like this" could only ever hand back your own catalogue. The content space (genre/era/
    audio) is where the pool lives, and _content_space widens it with them.

    Merged by RANK, not score: the two spaces are different metrics and their numbers are not
    comparable, but "this space's 1st pick" and "that space's 1st pick" are. Collaborative leads,
    since it is the stronger signal for tracks you own.
    """
    collab = neighbors(store, key, topn=topn)
    ckeys, CV, cidx = _content_space(store, include_new=True)
    content = (_rank(ckeys, CV, CV[cidx[key]], {key}, topn)
               if CV is not None and key in cidx else [])
    out, seen = [], {key}
    for pair in itertools.zip_longest(collab, content):
        for cand in pair:
            if cand is None or cand[0] in seen:
                continue
            seen.add(cand[0])
            out.append(cand)
            if len(out) >= topn:
                return out
    return out


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


# === Cluster expansion: blended collaborative + content rings for the Clusters canvas ===
CLUSTER_BETA = 0.6   # how hard a pruned ("negative model") track pushes a branch's ring away
SEED_FANOUT = 0.5    # for a MULTI-seed node, how much the ring is drawn to the NEAREST single seed
                     # vs the averaged centroid. >0 stops a minority seed (e.g. one psytrance pick
                     # among brit-rock) being averaged away. It reaches its own genre too. Self-
                     # adapting: when seeds are coherent (a focused path) max≈centroid, so it's a no-op.


def _branch_scores(pos_keys, neg_keys, beta, allkeys, M, index, fanout=SEED_FANOUT, groups=None,
                   artist_groups=None):
    """Per-key {key: pos_affinity - beta·cos(centroid_neg)} over one vector space, or None if no seed
    is present. pos_affinity blends proximity to the seeds' CENTROID with proximity to the NEAREST
    single seed (`fanout`, default SEED_FANOUT), so a multi-seed node reaches each seed's neighbourhood
    instead of only the average (a minority seed isn't averaged away). Shared by both the collaborative
    and content spaces in cluster_expand. With multiple central artists, use intersection affinity:
    each song must match the other artists, without its own artist inflating the score."""
    pi = [index[k] for k in pos_keys if k in index]
    if not pi:
        return None
    # An artist is one influence, regardless of how many songs we own by them. Ordinary branch
    # growth keeps its path weighting; the central ring supplies one group per selected seed.
    seeds = M[pi]
    owners = []
    artist_sets = {frozenset(g) for g in artist_groups or ()}
    groups = groups or artist_groups
    if groups:
        present = [g for g in groups if any(k in index for k in g)]
        centroids = [_normalize(M[[index[k] for k in g if k in index]].mean(0)) for g in present]
        owners = [set(g) if frozenset(g) in artist_sets else set() for g in present]
        if centroids:
            # Repeated positive keys are explicit emphasis (the canvas's fire button). Retain
            # those extra votes, plus any path keys outside the central artist/playlist groups.
            covered = {k for g in groups for k in g}
            for k in pos_keys:
                if k in covered:
                    covered.remove(k)
                elif k in index:
                    centroids.append(M[index[k]])
                    owners.append(set())
            seeds = np.asarray(centroids)
    pos = _normalize(seeds.mean(0))
    s = M @ pos
    if sum(bool(g) for g in owners) > 1:
        affinities = M @ seeds.T
        shared = np.min(affinities, axis=1)
        # A Shpongle song is ranked against Pet Shop Boys, and vice versa. With more artists,
        # the weakest remaining match defines the intersection. Collaborations belonging to every
        # artist keep their shared affinity rather than having no comparison left.
        for j, owned in enumerate(owners):
            for key in owned:
                if key in index:
                    affinities[index[key], j] = np.inf
        cross = np.min(affinities, axis=1)
        s = np.where(np.isfinite(cross), cross, shared)
    elif len(seeds) > 1 and fanout > 0.0:
        nearest = np.max(M @ seeds.T, axis=1)
        s = (1.0 - fanout) * s + fanout * nearest
    ni = [index[k] for k in neg_keys if k in index]
    if ni:
        neg = _normalize(M[ni].mean(0))
        s = s - beta * (M @ neg)
    return {allkeys[i]: float(s[i]) for i in range(len(allkeys))}


def _content_space(store, include_new):
    """Load the content (genre/era) vector space for cluster_expand. When include_new (Phase 2),
    widen it with the OUT-OF-CORPUS discovered-track pool, but only when they share the same model
    space (same column count). Returns (keys, V, idx)."""
    snapshot = store.rec_model.content_generation()
    ckeys, CV, cidx = load_content_vectors(store, snapshot=snapshot)
    if include_new:
        dkeys, DV, _ = load_discovered_content_vectors(store, snapshot=snapshot)
        if DV is not None:
            if CV is None:
                ckeys, CV = list(dkeys), DV
            elif DV.shape[1] == CV.shape[1]:         # same model space - safe to stack
                ckeys = list(ckeys) + list(dkeys)
                CV = np.vstack([CV, DV])
        cidx = {k: i for i, k in enumerate(ckeys)}
    return ckeys, CV, cidx


def percentile_scores(scores) -> dict:
    """#86 Map raw scores onto (0, 1] by ascending average rank (ties share their mean rank).

    The base for multiplicative taste weights: the old shift-by-min base let a single terrible
    candidate warp every reranked gap without bound. Ranks BOUND that influence (junk can never
    reorder the base itself), but they do not eliminate it entirely: percentile values still shift
    with pool size, so a multiplier sitting inside a pair's narrow crossover band can resolve
    differently in different pools. Accepted trade-off (owner-approved over softmax, which is
    pairwise pool-independent but needs a temperature constant we cannot tune without eval)."""
    if not scores:
        return {}
    n = len(scores)
    by_val = {}
    for k, s in scores.items():
        by_val.setdefault(s, []).append(k)
    out, taken = {}, 0
    for s in sorted(by_val):
        ks = by_val[s]
        avg_rank = (2 * taken + len(ks) + 1) / 2.0     # mean of ranks taken+1 .. taken+len(ks)
        for k in ks:
            out[k] = avg_rank / n
        taken += len(ks)
    return out


def _blend_spaces(collab_s, content_s, w):
    """Combine the collaborative and content per-key scores: (1-w)*collab + w*content where a key
    has both, else whichever space holds it. #86: each space is rank-normalized onto (0, 1] first
    (percentile_scores), so collab-only and content-only candidates compete on one comparable scale
    instead of raw cosines from distributions with different shapes."""
    cn, tn = percentile_scores(collab_s), percentile_scores(content_s)
    blended = {}
    for k in cn.keys() | tn.keys():
        has_c, has_t = k in cn, k in tn
        if has_c and has_t:
            blended[k] = (1.0 - w) * cn[k] + w * tn[k]
        elif has_c:
            blended[k] = cn[k]
        else:
            blended[k] = tn[k]
    return blended


def cluster_expand(store, pos_keys, neg_keys=(), exclude=None, topn=12, beta=None, allow=None,
                   include_new=False, seed_groups=None, candidate_seed_keys=(), artist_groups=None):
    """A Clusters-canvas ring: tracks nearest a node's PINNED-path centroid, tilted AWAY from the
    PRUNED set, scored as a blend of the collaborative (co-occurrence) embedding and the content
    (genre/era) vector. #86: each space's cosines are rank-normalized first, so the score is
    (1-w)·rank(cos(collab)) + w·rank(cos(content)), w = the cluster_content_weight knob. Each term is computed against the seeds' centroid in its own space; a candidate (or seed)
    missing one space is renormalized onto the other, so untagged tracks behave exactly as before
    and w=0 reproduces the pure-collaborative ring. pos/neg seeds and `exclude` are never returned.

    `beta` (prune strength) defaults to the `cluster_beta` knob; `cluster_seed_spread` sets the
    multi-seed fan-out. Both are read here so the Taste-model page can tune them without a rebuild.
    `allow`, when given, restricts candidates to a whitelist (#29 genre filter).
    `include_new` (Phase 2) widens the candidate pool with OUT-OF-CORPUS discovered tracks: they have
    no collaborative vector, so they're scored on the content term alone (in the same model space).
    Central artist seeds may explicitly keep their own songs eligible via `candidate_seed_keys`;
    `seed_groups` gives each selected artist/playlist/song equal influence on that opening ring.
    With multiple `artist_groups`, songs are scored against the OTHER artists; related tracks must
    match all the selected artists. Other central seeds and explicit emphasis remain targets too."""
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    w = float(rec_params.get_param(store, "cluster_content_weight"))
    if beta is None:
        beta = float(rec_params.get_param(store, "cluster_beta"))
    fanout = float(rec_params.get_param(store, "cluster_seed_spread"))
    ckeys, CV, cidx = _content_space(store, include_new)
    check_stopping()

    # Genre-only seed: a genre picked with no artist/song seed (empty pos_keys) seeds a cluster FROM the
    # genre itself: the centroid is the genre's own tracks, and we KEEP those tracks as candidates (don't
    # exclude the seed set) so the opening ring fills with the most representative tracks of the genre.
    seed_only = (not pos_keys) and bool(allow)
    src = list(allow) if seed_only else pos_keys
    collab_s = _branch_scores(src, neg_keys, beta, keys, V, idx, fanout, groups=seed_groups,
                              artist_groups=artist_groups) or {}
    content_s = ({} if (CV is None or w <= 0.0)
                 else (_branch_scores(src, neg_keys, beta, ckeys, CV, cidx, fanout, groups=seed_groups,
                                      artist_groups=artist_groups) or {}))
    blended = _blend_spaces(collab_s, content_s, w)
    check_stopping()

    preferred = set(candidate_seed_keys)
    excl = set(exclude or ()) | set(neg_keys) | (set() if seed_only else set(pos_keys) - preferred)
    allow = None if allow is None else set(allow)
    out = []
    for i, k in enumerate(sorted(blended, key=lambda k: (k not in preferred, -blended[k], k))):
        if i % 256 == 0:
            check_stopping()
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
    check_stopping()
    excl = set(path_keys) | {key} | set(exclude)
    bridge = None
    for i, j in enumerate(np.argsort(-both)):
        if i % 256 == 0:
            check_stopping()
        if keys[j] not in excl:
            bridge = keys[j]
            break
    return {"score": score, "bridge": bridge}
