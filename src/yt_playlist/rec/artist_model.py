"""#28 Artist-relationship model: relate artists by this user's co-curation (+ content + Last.fm edges).

Mirrors the track model (embed.py) at the artist level; the vocabulary is normalized artist names
(util.matching.normalize), the same keying new_artists already uses for the Last.fm cache. So far this
module implements the collaborative (co-occurrence) block (spec §A): reduce the track model's baskets to
distinct-artist sets and run them through the same PPMI + truncated SVD (embed._svd).

Model only: nothing in the recommendation/discovery surfaces consumes this yet (#28 is model-first).
"""
import json

import numpy as np

from yt_playlist.rec import embed, rec_params
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_map
from yt_playlist.util.matching import normalize

_ARTIST_CONTENT_MODEL_SETTING = "rec_artist_content_model"


def _artist_of(identity_key):
    """The (already-normalized) artist portion of an identity_key ('title|artist')."""
    return identity_key.rsplit("|", 1)[-1]


def artist_baskets(store):
    """Reduce the track model's co-occurrence baskets (playlists / albums / sessions / genre family /
    decade) to distinct-artist sets, so two artists relate when their tracks repeatedly co-occur.
    Baskets that collapse to a single artist (the per-artist basket, or a one-artist playlist) are
    dropped: they would only self-relate an artist. Returns a list of distinct normalized-artist lists."""
    out = []
    for basket in RecDao(store).rec_baskets():
        artists = {_artist_of(k) for k in basket if k}
        artists.discard("")
        if len(artists) >= 2:
            out.append(sorted(artists))
    return out


def build_collab_artist_vectors(store, dim=embed.DIM):
    """(artists, V): L2-normalized collaborative artist vectors from PPMI + truncated SVD over the
    artist baskets (reusing embed._svd, already generic over baskets of tokens). Empty when there's
    too little co-curation to model (fewer artists than the SVD needs)."""
    baskets = artist_baskets(store)
    artists = sorted({a for b in baskets for a in b})
    if len(artists) < dim + embed._MIN_VOCAB_MARGIN:
        return [], np.zeros((0, dim), dtype=np.float32)
    return embed._svd(baskets, artists, dim)


def build_collab_and_store(store, dim=embed.DIM) -> int:
    """Build and persist the collaborative artist vectors. Returns the number of artists embedded."""
    artists, V = build_collab_artist_vectors(store, dim)
    store.replace_rec_artist_vectors([(a, V[i].tobytes()) for i, a in enumerate(artists)])
    return len(artists)


def load_artist_vectors(store):
    """(artists, V, idx) from persisted collaborative artist vectors, or ([], None, {}) if none built."""
    return embed._load_vector_rows(store.get_rec_artist_vectors())


# --- §B content block: relate artists by what they ARE (genre family / subgenre / decade / audio) ---
def artist_content_profiles(store):
    """{normalized_artist: profile} aggregated over the artist's library tracks. profile =
    {'families': set, 'subs': set, 'decades': set, 'audio': {feat: mean}}. Grouped by the identity_key
    artist (already normalized), so it shares the §A vocabulary. The artist analogue of track_content."""
    dao = RecDao(store)
    prof: dict = {}

    def _p(artist):
        return prof.setdefault(artist, {"families": set(), "subs": set(), "decades": set(), "audio": {}})

    for k, (genre, year) in dao.track_content().items():
        p = _p(_artist_of(k))
        if genre:
            p["families"].add(genre_map.family(genre))
            sub = genre_map.subgenre(genre)
            if sub:
                p["subs"].add(sub)
        if year and str(year)[:4].isdigit():
            p["decades"].add(int(str(year)[:4]) // 10 * 10)

    accum: dict = {}   # artist -> {feat: [sum, count]}, for the per-artist audio mean
    for k, d in dao.track_audio_features().items():
        ac = accum.setdefault(_artist_of(k), {})
        for f in embed.CONTINUOUS_AUDIO:
            v = d.get(f)
            if v is not None:
                s = ac.setdefault(f, [0.0, 0]); s[0] += float(v); s[1] += 1
    for artist, ac in accum.items():
        _p(artist)["audio"] = {f: s[0] / s[1] for f, s in ac.items() if s[1]}
    return prof


def build_artist_content_model(profiles):
    """Shared artist-content space: a categorical token index (fam:/sub:/dec:) + per-audio-feature
    z-score stats across artists. Persisted so OUT-OF-CORPUS artists encode into the SAME space.
    Returns {'cat': {token: col}, 'ncat': int, 'cont': [[feat, mu, sd]]}. Mirrors embed.build_content_model."""
    cat: dict = {}

    def col(tok):
        return cat.setdefault(tok, len(cat))

    for p in profiles.values():
        for fam in p["families"]:
            col(f"fam:{fam}")
        for sub in p["subs"]:
            col(f"sub:{sub}")
        for dec in p["decades"]:
            col(f"dec:{dec}")
    cont = []
    for f in embed.CONTINUOUS_AUDIO:
        vals = [p["audio"][f] for p in profiles.values() if f in p.get("audio", {})]
        if len(vals) >= 2:
            mu = sum(vals) / len(vals)
            sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
            if sd > 0:
                cont.append([f, mu, sd])
    return {"cat": cat, "ncat": len(cat), "cont": cont}


def encode_artist_content(model, profile):
    """Encode one artist profile into `model`'s space -> L2-normalized float32 vector, or None when the
    profile has no feature the model knows. Multi-hot over the artist's families/subs/decades + their
    z-scored mean audio. Tokens absent from the model contribute 0 (graceful degradation)."""
    cat, ncat, cont = model["cat"], model["ncat"], model["cont"]
    vec = np.zeros(ncat + len(cont), dtype=np.float32)
    toks = ([f"fam:{x}" for x in profile.get("families", ())]
            + [f"sub:{x}" for x in profile.get("subs", ())]
            + [f"dec:{x}" for x in profile.get("decades", ())])
    for t in toks:
        c = cat.get(t)
        if c is not None:
            vec[int(c)] = 1.0
    audio = profile.get("audio", {})
    for j, (f, mu, sd) in enumerate(cont):
        v = audio.get(f)
        if v is not None and sd:
            vec[ncat + j] = embed.AUDIO_DIM_W * (float(v) - mu) / sd
    n = float(np.linalg.norm(vec))
    return (vec / n).astype(np.float32) if n > 0 else None


def _discovered_artist_profiles(store):
    """Genre-only content profiles for the out-of-corpus discovered-artist pool (from each entry's
    `genre` field), so OOC artists encode into the SAME artist-content space and become placeable."""
    out = {}
    for a in store.get_discovered_artists():
        artist, genre = normalize(a.get("artist") or ""), a.get("genre")
        if artist and genre:
            out[artist] = {"families": {genre_map.family(genre)}, "subs": set(), "decades": set(), "audio": {}}
    return out


def build_content_and_store(store) -> int:
    """Build the artist-content model + per-artist content vectors and persist both (vectors table +
    model JSON in settings). Covers in-corpus artists AND the out-of-corpus discovered-artist pool, all
    in one shared space. Returns the number of artists with a content vector."""
    profiles = artist_content_profiles(store)
    for artist, p in _discovered_artist_profiles(store).items():
        profiles.setdefault(artist, p)          # add OOC artists; keep the richer in-corpus profile if both
    model = build_artist_content_model(profiles)
    rows = []
    for artist in sorted(profiles):
        v = encode_artist_content(model, profiles[artist])
        if v is not None:
            rows.append((artist, v.tobytes()))
    store.replace_rec_artist_content_vectors(rows)
    store.set_setting(_ARTIST_CONTENT_MODEL_SETTING, json.dumps(model))
    return len(rows)


def load_artist_content_vectors(store):
    """(artists, V, idx) from persisted artist content vectors, or ([], None, {}) if none built."""
    return embed._load_vector_rows(store.get_rec_artist_content_vectors())


def _blended_set_scores(store, seeds):
    """{artist: blended relatedness} to a SET of `seeds` (all normalized). The §A collaborative and §B
    content cosine spaces (each to the seeds' centroid in that space) are combined via
    embed._blend_spaces (w = artist_content_weight; a candidate present in only one space falls back to
    it), then the §C Last.fm edges of the seeds are folded in as an additive bonus. The §C term reaches
    out-of-corpus artists with no §A/§B vector (they appear at their edge strength alone). {} when no
    seed is in any space and there are no edges. One seed = the single-artist neighbour case."""
    seeds = set(seeds)
    w = float(rec_params.get_param(store, "artist_content_weight"))
    artists, V, idx = load_artist_vectors(store)
    collab_s = {}
    si = [idx[a] for a in seeds if V is not None and a in idx]
    if si:
        sims = V @ embed._normalize(V[si].mean(0))
        collab_s = {artists[i]: float(sims[i]) for i in range(len(artists))}
    content_s = {}
    if w > 0.0:
        cart, CV, cidx = load_artist_content_vectors(store)
        ci = [cidx[a] for a in seeds if CV is not None and a in cidx]
        if ci:
            csims = CV @ embed._normalize(CV[ci].mean(0))
            content_s = {cart[i]: float(csims[i]) for i in range(len(cart))}
    blended = embed._blend_spaces(collab_s, content_s, w)
    w_edge = float(rec_params.get_param(store, "artist_edge_weight"))
    if w_edge > 0.0:
        for seed in seeds:
            for name, match in store.artist_similar_edges(seed):
                cand = normalize(name)
                if cand and cand not in seeds:
                    blended[cand] = blended.get(cand, 0.0) + w_edge * float(match)
    return blended


def _ranked(scores, exclude, topn):
    """Top-`topn` (key, score) from a score dict, descending, skipping `exclude`."""
    out = []
    for k in sorted(scores, key=lambda k: -scores[k]):
        if k in exclude:
            continue
        out.append((k, scores[k]))
        if len(out) >= topn:
            break
    return out


def artist_neighbors(store, artist, topn=12, exclude=None):
    """Artists most related to one seed by the blended (§A co-curation + §B content + §C edges) score.
    `artist` may be any casing; it is normalized to the model's vocabulary. Empty if the seed is in no
    space and has no edges (model unbuilt or seed unknown)."""
    seed = normalize(artist)
    return _ranked(_blended_set_scores(store, {seed}), (exclude or set()) | {seed}, topn)


def related_artists(store, seed_artists, topn=12, exclude_owned=True):
    """Artists related to a SET of seeds (e.g. a playlist's artists), by the blended seed-centroid
    score. `exclude_owned` drops artists already in the library, biasing toward discovery (#18); pass
    False for completion (#24), where owned related artists are exactly what you want."""
    seeds = {normalize(a) for a in seed_artists if a}
    excl = set(seeds)
    if exclude_owned:
        excl |= RecDao(store).library_artists()
    return _ranked(_blended_set_scores(store, seeds), excl, topn)


def artist_track_candidates(store, seed_artists, topn=24, include_out_of_corpus=True):
    """Expand the artists related to `seed_artists` into candidate TRACKS: owned tracks by those
    artists, plus (when include_out_of_corpus) out-of-corpus discovered tracks whose artist is related.
    Each dict carries key/title/artist/album/video_id/thumbnail (+ out_of_corpus=True for OOC). Powers
    'Complete this playlist' beyond the library (#24). Uses exclude_owned=False so owned related artists
    contribute their tracks."""
    related = {a for a, _ in related_artists(store, seed_artists, topn=topn, exclude_owned=False)}
    if not related:
        return []
    out = list(RecDao(store).tracks_by_artists(related))
    if include_out_of_corpus:
        for r in store.get_discovered_tracks():
            if normalize(r.get("artist") or "") in related:
                out.append({"key": r.get("identity_key"), "title": r.get("title"),
                            "artist": r.get("artist"), "album": r.get("album") or "",
                            "video_id": r.get("video_id"), "thumbnail": r.get("thumbnail"),
                            "out_of_corpus": True})
    return out


def build_artist_model_and_store(store, dim=embed.DIM) -> int:
    """Build and persist the full artist model: §A collaborative vectors + §B content model/vectors
    (the latter also covering the out-of-corpus discovered-artist pool). Returns the §A artist count.
    Driven on the same rebuild trigger as the track embedding (rec_worker._do_rebuild)."""
    n = build_collab_and_store(store, dim)
    build_content_and_store(store)
    return n
