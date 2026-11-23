"""Artist anchors and bridge ranking over supplied vectors and listening facts."""
import numpy as np
from yt_playlist.rec import embed


def anchor_values(matrix, index, seeds, refs, artists):
    if matrix is None:
        return [(artist, plays, None) for artist, plays in artists if artist and plays > 0]
    centre = embed._centroid(matrix, index, [(seeds, 1.)]) if seeds else None
    if centre is None:
        centre = matrix.mean(0)
        centre = centre / (np.linalg.norm(centre) + 1e-9)
    by_artist = {}
    for key in index:
        for artist in refs.get(key, ()):
            by_artist.setdefault(artist, []).append(index[key])
    out = []
    for artist, plays in artists:
        rows = by_artist.get(artist)
        if not rows:
            if artist and plays > 0:
                out.append((artist, plays, None))
            continue
        centroid = matrix[rows].mean(0)
        norm = np.linalg.norm(centroid)
        if norm == 0:
            continue
        unit = centroid / norm
        weight = plays * max(float(unit @ centre), 0.)
        if weight > 0:
            out.append((artist, weight, unit))
    return out


def rank_candidates(bridges, taste, dim):
    out = []
    for candidate, links in bridges.items():
        strength = sum(weight for _, weight, _ in links)
        proxy = np.zeros(dim, dtype=np.float64)
        for vector, weight, _ in links:
            if vector is not None:
                proxy += weight * vector
        if strength <= 0:
            continue
        fits = []
        if taste and np.any(proxy):
            contributions = taste.context_contributions(proxy)
            if contributions.sum() <= 0:
                continue
            order = np.argsort(-contributions)[:3]
            fits = [taste.titles[i] for i in order if contributions[i] > 0]
        because = [name for _, _, name in sorted(links, key=lambda item: -item[1])[:3]]
        out.append({'cand': candidate, 'score': round(float(strength), 4), 'because': because, 'fits': fits})
    out.sort(key=lambda candidate: (-candidate['score'], candidate['cand']))
    return out
