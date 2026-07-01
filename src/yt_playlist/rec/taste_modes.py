"""Taste modes (issue #60, Part A): discover the peaks of the user's multimodal taste in CONTENT
space (genre / era / audio), persist them with stable identity, expose them read-only on /taste.

Content space, not the co-occurrence embedding: the co-listen graph blends genres (you playlist
techno next to ambient, so they sit together there), while content vectors keep moods separable.
Fully automatic, no seeded genres."""
import numpy as np

from yt_playlist.rec import embed, rec_params
from yt_playlist.util import genre_map

_SEED = 1234567
_MAX_ITERS = 40
_MIN_CORPUS = 80


def _kmeanspp(X, k, seed):
    """k-means++ seeding + Lloyd iterations on L2-normalized rows X (n, d). Deterministic for a fixed
    (X, k, seed). Returns (labels (n,), centroids (k, d)). An emptied cluster keeps its centroid."""
    rng = np.random.Generator(np.random.PCG64(seed))
    n = X.shape[0]
    # k-means++ seeding.
    first = int(rng.integers(n))
    centers = [X[first]]
    d2 = ((X - centers[0]) ** 2).sum(axis=1)
    for _ in range(1, k):
        total = d2.sum()
        if total <= 0:
            centers.append(X[int(rng.integers(n))])
            continue
        nxt = int(rng.choice(n, p=d2 / total))
        centers.append(X[nxt])
        d2 = np.minimum(d2, ((X - X[nxt]) ** 2).sum(axis=1))
    C = np.array(centers, dtype=X.dtype)
    labels = np.zeros(n, dtype=int)
    for _ in range(_MAX_ITERS):
        # Assign by nearest center (cosine == dot here is not safe once centroids drift off the unit
        # sphere, so use euclidean, which is monotonic with cosine on normalized inputs anyway).
        dist = ((X[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
        new_labels = dist.argmin(axis=1)
        if np.array_equal(new_labels, labels):
            labels = new_labels
            break
        labels = new_labels
        for j in range(k):
            members = X[labels == j]
            if len(members):
                C[j] = members.mean(axis=0)
    return labels, C


def mode_label(families) -> str:
    """Display label from a member-majority family histogram [(family, count), ...] sorted desc.
    'house' for a single-dominant mode, 'house + techno' when the runner-up is at least half the top
    family's weight (a genuinely blended mode)."""
    if not families:
        return "mixed"
    top_fam, top_n = families[0]
    if len(families) > 1 and families[1][1] >= 0.5 * top_n:
        return f"{top_fam} + {families[1][0]}"
    return top_fam


def discover_modes(store, *, k=None, min_members=None, n_rep=6, seed=_SEED) -> list[dict]:
    """Cluster the library content vectors and return the dense clusters as taste modes. Each mode is
    {centroid, size, families, rep_keys, label}. Empty list when there are too few content vectors."""
    if k is None:
        k = rec_params.get_param(store, "modes_k")
    if min_members is None:
        min_members = rec_params.get_param(store, "modes_min_members")
    keys, V, _idx = embed.load_content_vectors(store)
    if V is None or len(keys) < max(int(min_members), _MIN_CORPUS):
        return []
    Vf = V.astype(np.float64)
    k = min(int(k), len(keys))
    labels, _centroids = _kmeanspp(Vf, k, seed)
    genres = store.modes.genres_for(keys)               # {identity_key: genre}
    modes = []
    for j in range(k):
        member_rows = np.where(labels == j)[0]
        if len(member_rows) < int(min_members):
            continue
        member_keys = [keys[i] for i in member_rows]
        centroid = Vf[member_rows].mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm == 0:
            continue
        centroid = (centroid / norm).astype(np.float32)
        # member-majority family histogram (NOT centroid-nearest, which lies)
        fam_counts = {}
        for mk in member_keys:
            g = genres.get(mk)
            if not g:
                continue
            fam = genre_map.family(g) or g
            fam_counts[fam] = fam_counts.get(fam, 0) + 1
        families = sorted(fam_counts.items(), key=lambda x: -x[1])[:4]
        # representative tracks: members nearest the centroid by cosine (rows are unit norm)
        sims = Vf[member_rows] @ centroid.astype(np.float64)
        order = np.argsort(-sims)[:n_rep]
        rep_keys = [member_keys[i] for i in order]
        modes.append({"centroid": centroid, "size": int(len(member_rows)),
                      "families": families, "rep_keys": rep_keys, "label": mode_label(families)})
    return modes


def reconcile(existing, discovered, *, threshold):
    """Assign stable mode_ids by greedy centroid-cosine matching. Matched discovered modes inherit the
    existing mode_id; unmatched get mode_id=None (the caller allocates ids); unmatched existing modes
    are returned as retired_ids. Deterministic. existing/discovered centroids are unit-norm."""
    pairs = []
    for di, d in enumerate(discovered):
        dc = np.asarray(d["centroid"], dtype=np.float64)
        for ei, e in enumerate(existing):
            cos = float(dc @ np.asarray(e["centroid"], dtype=np.float64))
            if cos >= threshold:
                pairs.append((cos, di, ei))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))   # deterministic: cos desc, then index order
    matched_d, matched_e, assign = set(), set(), {}
    for cos, di, ei in pairs:
        if di in matched_d or ei in matched_e:
            continue
        matched_d.add(di)
        matched_e.add(ei)
        assign[di] = existing[ei]["mode_id"]
    upserts = []
    for di, d in enumerate(discovered):
        u = dict(d)
        u["mode_id"] = assign.get(di)   # int if matched, else None
        upserts.append(u)
    retired_ids = [e["mode_id"] for ei, e in enumerate(existing) if ei not in matched_e]
    return upserts, retired_ids


def recompute(store, now, *, k=None, min_members=None) -> int:
    """Discover modes, reconcile against the persisted active modes, write the result. Returns the
    number of active modes written. k/min_members override the params (used by tests)."""
    discovered = discover_modes(store, k=k, min_members=min_members)
    if not discovered:
        # Nothing to cluster this pass (too few content vectors, or every cluster fell below
        # min_members). Keep the existing modes rather than reconcile-retiring ALL of them, which would
        # wipe the model and cascade to the bundles/cards. A genuinely empty library just stays empty.
        return len(store.modes.list_modes(active_only=True))
    existing = store.modes.list_modes(active_only=True)
    threshold = rec_params.get_param(store, "modes_match_threshold")
    upserts, retired = reconcile(existing, discovered, threshold=threshold)
    # Single owner of id allocation: fill mode_id=None upserts from a counter that spans retired rows
    # too, so a new mode never reuses a retired mode's id (history stays clean).
    nid = store.modes.next_mode_id()
    for u in upserts:
        if u["mode_id"] is None:
            u["mode_id"] = nid
            nid += 1
    store.modes.replace_modes(upserts, retired, now)
    return len(upserts)
