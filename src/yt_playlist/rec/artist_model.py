"""Artist relationships from local co-curation, content and provider evidence.

Mirrors collaborative track training at the artist level; the vocabulary uses canonical artist IDs.
Names are resolved only at display and provider boundaries. Collaborative vectors reduce the track
model's baskets to distinct-artist sets and use the same sparse PPMI and truncated SVD.
Content vectors and provider evidence contribute to the shared artist ranking.
The background model_refresh lifecycle owns fitting and coherent publication.
"""
from dataclasses import dataclass
from math import isfinite, sqrt

import numpy as np

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.artists import token
from yt_playlist.rec import (content_character, embedding_training as training,
                             embedding_vectors as vectors, rec_params, vector_math)
from yt_playlist.util.music_catalog import artist as catalog_artist
from yt_playlist.util.matching import normalize

YOUTUBE_WEIGHT = .20
PROJECTION_POLICY = 'ensemble-neighborhood-v1'
PROJECTION_ANCHORS = 32
PROJECTION_NEIGHBORS = 64


@dataclass(frozen=True)
class ArtistNeighbor:
    key: str
    score: float
    sources: dict[str, float]
    provider: dict | None


@dataclass(frozen=True)
class _EnsembleEvidence:
    collaborative: tuple
    content: tuple
    lastfm: dict
    content_weight: float
    edge_weight: float

    @classmethod
    def load(cls, store, seeds):
        content_weight = float(rec_params.get_param(store, 'artist_content_weight'))
        edge_weight = float(rec_params.get_param(store, 'artist_edge_weight'))
        return cls(
            load_artist_vectors(store),
            load_artist_content_vectors(store) if content_weight > 0 else ([], None, {}),
            store.artist_similarity.similar_edges_by_artist(seeds) if edge_weight > 0 else {},
            content_weight,
            edge_weight,
        )


def youtube_neighbors(page, *, limit=24, allow_names=False):
    """Read provider ranks from a detached artist page, keeping channel identities."""
    rows = (((page or {}).get('related') or {}).get('results') or [])[:limit]
    result = []
    for rank, row in enumerate(rows, 1):
        if choice := catalog_artist(row):
            result.append((rank, choice))
        elif allow_names and not (row.get('browseId') or row.get('channelId')) and row.get('title'):
            result.append((rank, {'name': row['title'], 'browse_id': None, 'thumbnail': ''}))
    return result


def youtube_artist_key(artists, browse_id):
    identity = artists.provider_identity(browse_id)
    return token(identity) if identity is not None else 'youtube:' + browse_id


def neighbor_reference(artists, neighbor):
    if neighbor.provider is not None and neighbor.provider.get('browse_id'):
        return neighbor.provider
    reference = artists.get_artist(neighbor.key)
    if reference is None:
        return neighbor.provider
    return {'name': reference['name'], 'browse_id': reference['provider_ids'].get('youtube'), 'thumbnail': ''}


def _provider_neighbors(artists, youtube):
    youtube = list(youtube)
    names = [ref['name'] for _, ref in youtube if not ref['browse_id']]
    matches = artists.name_matches(names) if names else {}
    for rank, ref in youtube:
        if browse := ref['browse_id']:
            yield youtube_artist_key(artists, browse), rank, ref
        elif len(matches[ref['name']]) <= 1:
            identity = next(iter(matches[ref['name']]), None)
            key = token(identity) if identity is not None else 'youtube-name:' + normalize(ref['name'])
            yield key, rank, ref


def artist_baskets(store):
    """Reduce the track model's co-occurrence baskets (playlists / albums / sessions / genre family /
    decade) to distinct-artist sets, so two artists relate when their tracks repeatedly co-occur.
    Baskets that collapse to a single artist (the per-artist basket, or a one-artist playlist) are
    dropped: they would only self-relate an artist. Returns a list of distinct normalized-artist lists."""
    refs = store.artists.track_keys(library=True)
    return artist_baskets_from_tracks(store.rec_evidence.rec_baskets(), refs)


def artist_baskets_from_tracks(baskets, refs):
    out = []
    for basket in baskets:
        artists = {a for k in basket for a in refs.get(k,())}
        artists.discard("")
        if len(artists) >= 2:
            out.append(sorted(artists))
    return out


def build_collab_artist_vectors(store, dim=training.DIM):
    """(artists, V): L2-normalized collaborative artist vectors from PPMI + truncated SVD over the
    artist baskets (reusing training.svd, already generic over baskets of tokens). Empty when there's
    too little co-curation to model (fewer artists than the SVD needs)."""
    return fit_artist_baskets(artist_baskets(store), dim)


def fit_artist_baskets(baskets, dim=training.DIM):
    artists = sorted({a for b in baskets for a in b})
    if len(artists) < dim + training.MIN_VOCAB_MARGIN:
        return [], np.zeros((0, dim), dtype=np.float32)
    return training.svd(baskets, artists, dim)


def load_artist_vectors(store):
    """(artists, V, idx) from persisted collaborative artist vectors, or ([], None, {}) if none built."""
    return vectors.load_vector_rows(store.rec_model.get_rec_artist_vectors())


# --- §B content block: relate artists by what they ARE (genre family / subgenre / decade / audio) ---
def artist_content_profiles(store):
    """Canonical artist profiles over distinct owned and discovered recordings."""
    inputs, discoveries, character = vectors.capture_content_inputs(store, store.discovery.get_discovered_tracks())
    axes = store.genres.feature_axes([*(genre for genre, _ in inputs.content.values()),
                                     *(row.get('genre') for row in discoveries)])
    return profiles_from_recordings(store, inputs, discoveries, character, axes)


def profiles_from_recordings(store, inputs, discoveries, character, axes):
    content = {row['identity_key']: (row.get('genre'), row.get('year')) for row in discoveries}
    content.update({key: inputs.content.get(key, (None, None)) for key in inputs.keys})
    refs = store.artists.track_keys(keys=character)
    return profiles_from_tracks(content, character, refs, axes)


def profiles_from_tracks(content, character, refs, axes):
    profiles, sums, counts = {}, {}, {}
    for key in sorted(character):
        genre, year = content.get(key, (None, None))
        for artist in set(refs.get(key, ())):
            p = profiles.setdefault(artist, {"families": set(), "subs": set(), "decades": set(), "character": {}})
            counts[artist] = counts.get(artist, 0) + 1
            if genre:
                family, sub = axes[genre]
                p["families"].add(family)
                if sub:
                    p["subs"].add(sub)
            if year and str(year)[:4].isdigit():
                p["decades"].add(int(str(year)[:4]) // 10 * 10)

            acc = sums.setdefault(artist, {})
            for name, (value, support) in character[key][1].items():
                total = acc.setdefault(name, [0., 0.])
                total[0] += value * support
                total[1] += support
    for artist, acc in sums.items():
        profiles[artist]['character'] = {name: (weighted / support, support / counts[artist])
                                         for name, (weighted, support) in acc.items()}
    return profiles


def build_artist_content_model(profiles):
    """Fit one genre/era vocabulary with the shared recording character axes."""
    cat: dict = {}

    def col(tok):
        return cat.setdefault(tok, len(cat))

    for _, p in sorted(profiles.items()):
        for fam in sorted(p["families"]):
            col(f"fam:{fam}")
        for sub in sorted(p["subs"]):
            col(f"sub:{sub}")
        for dec in sorted(p["decades"]):
            col(f"dec:{dec}")
    return {"cat": cat, "ncat": len(cat), "cont": list(content_character.COLUMNS),
            "policy": content_character.VERSION}


def encode_artist_content(model, profile):
    """Project an artist's known catalog and character properties into the shared space."""
    cat, ncat, cont = model["cat"], model["ncat"], model["cont"]
    vec = np.zeros(ncat + len(cont), dtype=np.float32)
    toks = ([f"fam:{x}" for x in profile.get("families", ())]
            + [f"sub:{x}" for x in profile.get("subs", ())]
            + [f"dec:{x}" for x in profile.get("decades", ())])
    for t in toks:
        c = cat.get(t)
        if c is not None:
            vec[int(c)] = 1.0
    content_character.encode(cont, profile.get("character", {}), vec[ncat:])
    return content_character.finish_vector(vec, model.get('policy'))


def _discovered_artist_profiles(store):
    """Genre-only content profiles for the out-of-corpus discovered-artist pool (from each entry's
    `genre` field), so OOC artists encode into the SAME artist-content space and become placeable."""
    discovered = store.discovery.get_discovered_artists()
    axes = store.genres.feature_axes({a.get('genre') for a in discovered})
    artists = {a.get('artist_id') or a.get('artist'): store.artists.key(a.get('artist_id') or a.get('artist'))
               for a in discovered}
    return discovered_profiles_from_inputs(discovered, axes, artists)


def discovered_profiles_from_inputs(discovered, axes, artists):
    out = {}
    for a in discovered:
        artist, genre = artists.get(a.get('artist_id') or a.get('artist')), a.get('genre')
        if artist and genre:
            family, sub = axes[genre]
            out[artist] = {"families": {family}, "subs": {sub} if sub else set(), "decades": set(), "character": {}}
    return out


def load_artist_content_vectors(store):
    """(artists, V, idx) from persisted artist content vectors, or ([], None, {}) if none built."""
    return vectors.load_vector_rows(store.rec_model.get_rec_artist_content_vectors())


def _cosines(seeds, space):
    keys, matrix, index = space
    rows = [index[key] for key in sorted(seeds) if key in index]
    if matrix is None or not rows:
        return {}
    scores = matrix @ vector_math.normalize(matrix[rows].mean(0))
    return dict(zip(keys, map(float, scores)))


def rank_neighbors(seeds, collaborative, content, lastfm, youtube, *, content_weight, edge_weight):
    """Rank cosine percentiles, bounded Last.fm matches and inverse-root provider ranks."""
    collab = _cosines(seeds, collaborative)
    musical = _cosines(seeds, content)
    co_rank, music_rank = vector_math.percentile_scores(collab), vector_math.percentile_scores(musical)
    local = co_rank | music_rank
    for key in co_rank.keys() & music_rank.keys():
        local[key] = (1 - content_weight) * co_rank[key] + content_weight * music_rank[key]
    matches, positions, references = {}, {}, {}
    for key, match in lastfm:
        if key and edge_weight > 0 and isfinite(match) and match > 0:
            matches[key] = max(matches.get(key, 0.), min(match, 1.))
    for key, rank, reference in youtube:
        score = 1 / sqrt(rank)
        if score > positions.get(key, 0.):
            positions[key], references[key] = score, reference

    result = []
    provider_scale = .65 if local.keys() - seeds else 1.
    for key in (local.keys() | matches.keys() | positions.keys()) - seeds:
        match, position = matches.get(key, 0.), positions.get(key, 0.)
        bonus = edge_weight * match + YOUTUBE_WEIGHT * position
        if key in local:
            score = local[key] + bonus
        else:
            # A candidate from one provider remains useful without either local vector.
            # When local ranks exist, discount uncorroborated provider-only strength.
            available = edge_weight * bool(match) + YOUTUBE_WEIGHT * bool(position)
            score = bonus / available * provider_scale
        sources = {name: values[key] for name, values in (
            ('co-occurrence', co_rank), ('content', music_rank), ('lastfm', matches), ('youtube', positions))
                   if key in values}
        result.append(ArtistNeighbor(key, score, sources, references.get(key)))
    return sorted(result, key=lambda neighbor: (-neighbor.score, -len(neighbor.sources), neighbor.key))


def ranked_artist_neighbors(store, seed_artists, *, youtube=(), exclude=(), exclude_owned=False, topn=12,
                            _evidence=None):
    """Rank canonical neighbors with optional artist-page evidence from the calling feature."""
    seeds = {store.artists.key(artist) or artist for artist in seed_artists if artist}
    excluded = {store.artists.key(artist) or artist for artist in exclude if artist}
    if exclude_owned:
        excluded |= {artist for refs in store.artists.track_keys(library=True).values() for artist in refs}
    evidence = _EnsembleEvidence.load(store, seeds) if _evidence is None else _evidence
    lastfm = (pair for seed in seeds for pair in evidence.lastfm.get(seed, ()))
    ranked = rank_neighbors(seeds, evidence.collaborative, evidence.content, lastfm,
        _provider_neighbors(store.artists, youtube),
        content_weight=evidence.content_weight, edge_weight=evidence.edge_weight)
    return [neighbor for neighbor in ranked if neighbor.key not in excluded][:topn]


def projection_features(store, artist_counts):
    """Detached scores to frequently credited training artists in a fixed feature order."""
    anchors = sorted(artist_counts, key=lambda key: (-artist_counts[key], key))[:PROJECTION_ANCHORS]
    evidence = _EnsembleEvidence.load(store, anchors)
    columns = []
    for anchor in anchors:
        check_cancellable_work()
        neighbors = ranked_artist_neighbors(store, [anchor], topn=PROJECTION_NEIGHBORS, _evidence=evidence)
        if neighbors:
            columns.append({anchor: 1., **{neighbor.key: neighbor.score for neighbor in neighbors}})
    features = {}
    for column, scores in enumerate(columns):
        for key, score in scores.items():
            if key not in features:
                features[key] = np.zeros(len(columns), dtype=np.float32)
            features[key][column] = score
    return features


def artist_neighbors(store, artist, topn=12, exclude=None):
    """Compatibility view of the shared ranking with public artist keys."""
    ranked = ranked_artist_neighbors(store, [artist], exclude=exclude or (), topn=topn)
    public = store.artists.public_keys(neighbor.key for neighbor in ranked)
    return [(public[neighbor.key], neighbor.score) for neighbor in ranked]


def related_artist_scores(store, seed_artists, topn=12, exclude_owned=True, *, youtube=()):
    """Canonical artist keys mapped to blended scores, in descending rank order.

    Exclude seeds, and optionally owned artists for discovery. Candidate expansion
    retains these identities so artists sharing a display name stay distinct.
    """
    neighbors = ranked_artist_neighbors(store, seed_artists, youtube=youtube, exclude_owned=exclude_owned, topn=topn)
    return {neighbor.key: neighbor.score for neighbor in neighbors}


def artist_track_candidates(store, seed_artists, topn=24, include_out_of_corpus=True, *,
                            scores=None, artist_refs=None):
    """Expand the artists related to `seed_artists` into candidate TRACKS: owned tracks by those
    artists, plus (when include_out_of_corpus) out-of-corpus discovered tracks whose artist is related.
    Each dict carries key/title/artist/album/video_id/thumbnail (+ out_of_corpus=True for OOC). Powers
    'Complete this playlist' beyond the library (#24). Each candidate has an artist_score. Callers
    can share canonical scores and a credit snapshot; otherwise they are loaded once here."""
    if scores is None:
        scores = related_artist_scores(store, seed_artists, topn=topn, exclude_owned=False)
    if not scores:
        return []
    out = list(store.rec_query.tracks_by_artists(scores))
    refs = store.artists.track_keys() if artist_refs is None else artist_refs
    for row in out:
        row["artist_score"] = max((scores.get(a,0) for a in refs.get(row['key'],())),default=0)
    if include_out_of_corpus:
        discovery_scores = {}
        for key in store.discovery.discovered_track_keys():
            matches = [scores[artist] for artist in refs.get(key, ()) if artist in scores]
            if matches:
                discovery_scores[key] = max(matches)
        for key, row in store.discovery.playable_discoveries_by_keys(discovery_scores).items():
            out.append({"key": key, "title": row["title"], "artist": row["artist"],
                        "album": row["album"] or "", "video_id": row["video_id"],
                        "thumbnail": row["thumbnail"], "genre": row["genre"] or "",
                        "artist_score": discovery_scores[key], "out_of_corpus": True})
    return out
