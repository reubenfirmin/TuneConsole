"""Taste-pinned new-artist discovery.

External sources (Last.fm) supply similarity *edges*; our embedding + play-weighted per-playlist
taste model supply the *judgement*. A candidate new artist is grounded by a match-weighted centroid
of the user's artists it is similar to (the bridge anchors), then scored against the play-weighted
per-playlist taste — so it only surfaces if it fits contexts the user actually plays, and a low-play
playlist can't drag in off-taste artists. Each result explains itself: which of your artists bridged
it, and which of your playlists it fits. Runs in the background worker; Last.fm results cached 14d.
"""
import numpy as np

from yt_playlist import embed, lastfm, recommend
from yt_playlist.matching import normalize
from yt_playlist.rec_dao import RecDao


def _anchors(store, V, idx, top_n=30):
    """[(display_name, weight, unit_vector)] — your artists weighted by play × taste-centrality, so
    the *core* of your taste anchors discovery (not one-off saves)."""
    seeds = store.top_played_keys(limit=12)
    centre = embed._centroid(V, idx, [(seeds, 1.0)]) if seeds else None
    if centre is None:
        centre = V.mean(0)
        centre = centre / (np.linalg.norm(centre) + 1e-9)
    by_artist = {}
    for k in idx:
        by_artist.setdefault(k.rsplit("|", 1)[-1], []).append(idx[k])
    out = []
    for a in store.top_artists(top_n):
        rows = by_artist.get(normalize(a["artist"]))
        if not rows:
            continue
        c = V[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        unit = c / n
        weight = a["plays"] * max(float(unit @ centre), 0.0)
        if weight > 0:
            out.append((a["artist"], weight, unit))
    return out


def new_artists(ctx, limit=15, max_anchors=30):
    """Taste-pinned new artists: [{artist, score, because[anchors], fits[playlists]}], or []."""
    store = ctx.store
    key = lastfm.api_key(store)
    if not key:
        return []
    pt = recommend.playlist_taste(store)
    if not pt:
        return []
    keys, V, idx = embed.load_vectors(store)
    anchors = _anchors(store, V, idx, top_n=max_anchors)
    if not anchors:
        return []
    dao = RecDao(store)
    owned = dao.library_artists()
    now = ctx.now_fn()

    def fetch(name):
        cached = dao.cached_similar(name, now)
        if cached is None:
            cached = [[n, m] for n, m in lastfm.similar_artists(name, key)]
            dao.cache_similar(name, cached, now)
        return cached

    bridges = {}   # candidate -> [(anchor_unit_vec, edge_weight, anchor_name), ...]
    for name, weight, vec in anchors:
        for cand, match in fetch(name):
            if not cand or normalize(cand) in owned:
                continue
            bridges.setdefault(cand, []).append((vec, weight * float(match), name))

    out = []
    for cand, bl in bridges.items():
        strength = sum(w for _, w, _ in bl)                 # collaborative: Σ anchor_weight × match
        proxy = np.zeros(V.shape[1], dtype=np.float64)
        for v, w, _ in bl:
            proxy += w * v                                  # edge-weighted bridge centroid
        if strength <= 0 or not np.any(proxy):
            continue
        taste, fits = pt.score(proxy)                       # play-weighted per-playlist fit (direction)
        score = taste * strength                            # judged-by-taste × bridge-strength
        because = [n for _, _, n in sorted(bl, key=lambda x: -x[1])[:3]]
        out.append({"artist": cand, "score": round(float(score), 4),
                    "because": because, "fits": [t for t, _ in fits]})
    out.sort(key=lambda c: -c["score"])
    return out[:limit]
