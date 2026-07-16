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
    {centroid, space, size, families, rep_keys, label}. `space` fingerprints the content space the
    centroid lives in, so reconcile can tell a stale centroid from a comparable one. Empty list when
    there are too few content vectors."""
    if k is None:
        k = rec_params.get_param(store, "modes_k")
    if min_members is None:
        min_members = rec_params.get_param(store, "modes_min_members")
    space = embed.content_space_id(store)
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
        # member_keys is transient: reconcile uses it to carry mode identity across a content-space
        # rebuild (an existing mode's rep_keys must land inside this cluster). replace_modes reads only
        # the named columns, so it never reaches the database.
        modes.append({"centroid": centroid, "space": space, "size": int(len(member_rows)),
                      "families": families, "rep_keys": rep_keys, "label": mode_label(families),
                      "member_keys": member_keys})
    return modes


def live_modes(store):
    """Active modes whose centroids live in the CURRENT content space.

    The one safe way to read persisted centroids. `enrich_worker` can rebuild the content space
    without recomputing modes, so at any moment the stored centroids may belong to a space that no
    longer exists; their columns then mean something else entirely. Callers that stack centroids
    against live content vectors must go through here, not `store.modes.list_modes`, or they are one
    enrichment batch away from either a shape error or a silently wrong cosine.
    """
    space = embed.content_space_id(store)
    return [m for m in store.modes.list_modes(active_only=True) if (m.get("space") or "") == space]


REP_MATCH_THRESHOLD = 0.5    # Fraction of an existing mode's representative tracks that must land
                             # inside a discovered cluster for the two to be the same taste region.
                             # A simple majority: below that, a fresh id is more honest.


def _rep_containment(existing_reps, discovered_keys) -> float:
    """What fraction of an existing mode's representative tracks fall inside a discovered cluster.

    Containment, NOT Jaccard. rep_keys holds 6 tracks; a discovered cluster holds hundreds (90 to 691
    on the reference library), so Jaccard is dominated by the size asymmetry and reads ~0.01 even when
    every single representative is inside the cluster. Measured on the reference database: median
    Jaccard 0.01 (0/13 modes above 0.34), median containment 1.00 (12/13 above 0.67).

    identity_keys do not depend on the embedding basis, which is what lets this survive a content-space
    rebuild when centroid cosine cannot.
    """
    reps = set(existing_reps or ())
    keys = set(discovered_keys or ())
    if not reps or not keys:
        return 0.0
    return len(reps & keys) / len(reps)


def reconcile(existing, discovered, *, threshold):
    """Assign stable mode_ids. Matched discovered modes inherit the existing mode_id; unmatched get
    mode_id=None (the caller allocates ids); unmatched existing modes are returned as retired_ids.
    Deterministic. Centroids are unit-norm.

    Two kinds of evidence, in strict priority order:

    * TIER 0, same content space: centroid cosine >= `threshold`. The centroid is the best available
      description of a mode, but it means nothing outside the space that built it, and that space is
      rebuilt whenever enrichment adds a genre/key token (see embed.content_model_fingerprint).
      Scoring a cross-space pair by cosine either crashes on a dimension mismatch or, worse, silently
      matches modes through a reordered basis.
    * TIER 1, across a rebuild: >= REP_MATCH_THRESHOLD of the existing mode's `rep_keys` fall inside
      the discovered cluster's membership. identity_keys are independent of the basis. Without this
      tier every mode_id is reallocated whenever the space changes, which silently discards the picks,
      impressions, and Thompson posterior keyed to the old id (mode_eval.mode_bandit_stats feeds
      mode_surfaces.thompson_mode_scores, which looks up stats by LIVE mode_id and finds nothing).

    A same-space match always beats a cross-space one, so tier is the primary sort key. When several
    existing modes contain into one discovered cluster (the space merged them), greedy assignment gives
    the id to the best-contained one and retires the rest, which is the honest outcome: one region now
    where there were two.
    """
    pairs = []
    for di, d in enumerate(discovered):
        dc = np.asarray(d["centroid"], dtype=np.float64)
        # Prefer the discovered cluster's full membership. discover_modes supplies it; a caller that
        # only has rep_keys (tests, and any future consumer reading modes back off disk) degrades to
        # comparing against those instead.
        d_keys = d.get("member_keys") or d.get("rep_keys")
        for ei, e in enumerate(existing):
            if e.get("space") == d.get("space"):
                ec = np.asarray(e["centroid"], dtype=np.float64)
                if ec.shape != dc.shape:    # same fingerprint, different shape: cannot happen, but a
                    continue                # silent skip beats a ValueError in a best-effort worker
                cos = float(dc @ ec)
                if cos >= threshold:
                    pairs.append((0, -cos, di, ei))
            else:
                c = _rep_containment(e.get("rep_keys"), d_keys)
                if c >= REP_MATCH_THRESHOLD:
                    pairs.append((1, -c, di, ei))
    pairs.sort()            # tier asc (same-space first), score desc, then index order: deterministic
    matched_d, matched_e, assign = set(), set(), {}
    for _tier, _neg_score, di, ei in pairs:
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
