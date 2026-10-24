"""Neighbor retrieval, blended cluster expansion and connection geometry.

Training algorithms live in embedding_training, content vocabulary/identity in
content_model, generation reads in embedding_vectors, and shared numeric rules in
vector_math. expand_spaces accepts detached spaces and explicit policy parameters.
"""
import itertools

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import embedding_vectors as vectors, rec_params, vector_math
from yt_playlist.rec.embedding_vectors import VectorSpace

CLUSTER_BETA = 0.6
SEED_FANOUT = 0.5


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
    """Tracks most similar to one seed track in the served collaborative space."""
    keys, V, idx = vectors.load_vectors(store)
    if V is None or key not in idx:
        return []
    return _rank(keys, V, V[idx[key]], (exclude or set()) | {key}, topn)


def neighbors_blended(store, key, topn=12):
    """Interleave collaborative/content neighbors by rank, including discovery."""
    collab = neighbors(store, key, topn=topn)
    ckeys, CV, cidx = vectors.content_space(store, include_new=True)
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
    """Proxy an unmodeled seed through recordings sharing any canonical artist credit.

    Each modeled recording contributes once, even when several seed credits match.
    Without stored seed credits or matching modeled work there is no artist proxy.
    """
    keys, V, idx = vectors.load_vectors(store)
    if V is None or key in idx:
        return []
    credits = store.artists.track_keys()
    artists = credits.get(key, set())
    if not artists:
        return []
    rows = [idx[k] for k in idx if artists.intersection(credits.get(k, ()))]
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


def _branch_scores(pos_keys, neg_keys, beta, allkeys, M, index, fanout=SEED_FANOUT, groups=None,
                   artist_groups=None):
    """Seed affinity minus negative-centroid affinity within a single vector space.

    Fanout preserves minority seeds. Central artists receive equal influence;
    each artist's own songs must match the other selected artists.
    """
    pi = [index[k] for k in pos_keys if k in index]
    if not pi:
        return None
    seeds = M[pi]
    owners = []
    artist_sets = {frozenset(g) for g in artist_groups or ()}
    groups = groups or artist_groups
    if groups:
        present = [g for g in groups if any(k in index for k in g)]
        centroids = [vector_math.normalize(M[[index[k] for k in g if k in index]].mean(0)) for g in present]
        owners = [set(g) if frozenset(g) in artist_sets else set() for g in present]
        if centroids:
            # Repeated keys are explicit emphasis; retain votes beyond each group's first key.
            covered = {k for g in groups for k in g}
            for k in pos_keys:
                if k in covered:
                    covered.remove(k)
                elif k in index:
                    centroids.append(M[index[k]])
                    owners.append(set())
            seeds = np.asarray(centroids)
    pos = vector_math.normalize(seeds.mean(0))
    s = M @ pos
    if sum(bool(g) for g in owners) > 1:
        affinities = M @ seeds.T
        shared = np.min(affinities, axis=1)
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
        neg = vector_math.normalize(M[ni].mean(0))
        s = s - beta * (M @ neg)
    return {allkeys[i]: float(s[i]) for i in range(len(allkeys))}


def cluster_expand(store, pos_keys, neg_keys=(), exclude=None, topn=12, beta=None, allow=None,
                   include_new=False, seed_groups=None, candidate_seed_keys=(), artist_groups=None):
    """Read existing spaces and saved cluster controls; never fit a model on a request."""
    collab = vectors.load_vectors(store)
    if collab[1] is None:
        return []
    w = float(rec_params.get_param(store, "cluster_content_weight"))
    if beta is None:
        beta = float(rec_params.get_param(store, "cluster_beta"))
    fanout = float(rec_params.get_param(store, "cluster_seed_spread"))
    content = vectors.content_space(store, include_new)
    return expand_spaces(collab, content, pos_keys, neg_keys, exclude, topn, beta, allow,
                         w=w, fanout=fanout, seed_groups=seed_groups,
                         candidate_seed_keys=candidate_seed_keys, artist_groups=artist_groups)


def expand_spaces(collab: VectorSpace, content: VectorSpace, pos_keys, neg_keys=(), exclude=None,
                  topn=12, beta=CLUSTER_BETA, allow=None, *, w, fanout=SEED_FANOUT,
                  seed_groups=None, candidate_seed_keys=(), artist_groups=None):
    """Rank a cluster from detached generation inputs and explicit blend/seed policy.

    Genre-only rings retain their seeds as candidates. Preferred central songs
    remain eligible; other positive/negative seeds and caller exclusions do not.
    Rank-normalize each space before blending so discovery competes fairly.
    """
    keys, V, idx = collab
    if V is None:
        return []
    ckeys, CV, cidx = content
    check_stopping()
    seed_only = (not pos_keys) and bool(allow)
    src = list(allow) if seed_only else pos_keys
    collab_s = _branch_scores(src, neg_keys, beta, keys, V, idx, fanout, groups=seed_groups,
                              artist_groups=artist_groups) or {}
    content_s = ({} if (CV is None or w <= 0.0)
                 else (_branch_scores(src, neg_keys, beta, ckeys, CV, cidx, fanout, groups=seed_groups,
                                      artist_groups=artist_groups) or {}))
    blended = vector_math.blend_spaces(collab_s, content_s, w)
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
    """Child/path cosine and the modeled track bridging both ends of a cluster edge."""
    keys, V, idx = vectors.load_vectors(store)
    if V is None or key not in idx:
        return {"score": None, "bridge": None}
    pi = [idx[k] for k in path_keys if k in idx and k != key]
    if not pi:
        return {"score": None, "bridge": None}
    p = V[pi].mean(0)
    p = vector_math.normalize(p)
    c = V[idx[key]]
    score = float(c @ p)
    both = np.minimum(V @ c, V @ p)
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
