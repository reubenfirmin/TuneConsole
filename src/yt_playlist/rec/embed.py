"""Track neighbor retrieval and collaborative Cluster connection geometry."""
import itertools

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import embedding_vectors as vectors, vector_math
from yt_playlist.rec.embedding_vectors import VectorSpace


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


def track_neighbors(store, key, topn=12):
    """Retrieve the blended neighbors, then try the canonical artist proxy."""
    if topn <= 0:
        return []
    return neighbors_blended(store, key, topn=topn) or neighbors_for_unmodeled(store, key, topn=topn)


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


def connection_geometry(store, key, path_keys, exclude=()):
    """Child/path cosine and the modeled track bridging both ends of a cluster edge."""
    return geometry(vectors.load_vectors(store), key, path_keys, exclude)


def geometry(space: VectorSpace, key, path_keys, exclude=()):
    """Child/path cosine and bridge from one captured vector generation."""
    keys, V, idx = space
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
        if not both[j] > 0:
            break
        if keys[j] not in excl:
            bridge = keys[j]
            break
    return {"score": score, "bridge": bridge}
