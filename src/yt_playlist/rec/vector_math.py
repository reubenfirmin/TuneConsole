"""Shared numeric rules for independent track/artist model and retrieval owners."""
import numpy as np

NORM_EPS = 1e-9


def normalize(v):
    """L2-normalize one vector; degenerate zero vectors remain finite."""
    return v / (np.linalg.norm(v) + NORM_EPS)


def rank_by_context(keys, vectors, context, eligible, k=20):
    """Select candidates nearest the context centroid, with stable identity ties."""
    idx = {key: i for i, key in enumerate(keys)}
    context = sorted(context & set(idx))
    if not context or not eligible:
        return set()
    centroid = vectors[[idx[key] for key in context]].mean(0)
    norm = np.linalg.norm(centroid)
    if norm < NORM_EPS:
        return set()
    scores = vectors @ (centroid / norm)
    return set(sorted(eligible, key=lambda key: (-float(scores[idx[key]]), key))[:k])


def percentile_scores(scores) -> dict:
    """Ascending average ranks in (0, 1]; tied raw scores share their mean rank.

    Ranks bound the influence of extreme values before multiplicative taste
    weights. They intentionally still depend on the candidate pool's size.
    """
    if not scores:
        return {}
    n = len(scores)
    by_val = {}
    for k, s in scores.items():
        by_val.setdefault(s, []).append(k)
    out, taken = {}, 0
    for s in sorted(by_val):
        ks = by_val[s]
        avg_rank = (2 * taken + len(ks) + 1) / 2.0
        for k in ks:
            out[k] = avg_rank / n
        taken += len(ks)
    return out


def blend_spaces(collab_s, content_s, w):
    """Blend comparable percentile ranks; keys in only one space retain its score."""
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
