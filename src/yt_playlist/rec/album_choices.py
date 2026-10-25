"""Album experiments prepared by the shared Home/model worker.

Release features use the same content basis and interests as track recommendations. The request
only filters/rotates the prepared pool. Missing metadata stays explicitly unscored.
"""
import json
import logging
import math
import re
from collections import Counter

import numpy as np

from yt_playlist.rec import content_model, embedding_vectors, layers, rec_params, scoring
from yt_playlist.util.matching import normalize, recording_title
from yt_playlist.util.years import decade

SURFACE = 'album_prepared'
MAX_AGE_S = 1800
_LOG = logging.getLogger(__name__)


def load(store, now, schedule=lambda: None):
    """Serve the last prepared album set without depending on Mix tapes readiness."""
    try:
        payload = store.rec_proposals.get_proposals(SURFACE)
        if (not isinstance(payload, dict) or not isinstance(payload.get('items'), list)
                or not isinstance(payload.get('context'), dict)
                or not isinstance(payload.get('built_at'), (int, float))
                or not math.isfinite(payload['built_at'])
                or any(not isinstance(item, dict) or not isinstance(item.get('browse_id'), str)
                       for item in payload['items'])):
            payload = None
    except (ValueError, TypeError):
        payload = None
    if payload is None or now - payload['built_at'] >= MAX_AGE_S:
        schedule()
    from yt_playlist.rec import product_comparison
    from yt_playlist.rec.home_cards import _epoch
    epoch = _epoch(store, 'album_choices')
    held = store.rec_proposals.get_proposals('model_album_assignment') or {}
    previous = held.get('comparison') if held.get('epoch') == epoch else None
    paired, comparison = product_comparison.prepared(store, 'albums', epoch, now, existing=previous)
    if paired:
        payload = {**paired, 'context': {**paired['context'], 'comparison': comparison}}
        if comparison != previous:
            store.rec_proposals.put_proposals('model_album_assignment', {'epoch': epoch, 'comparison': comparison}, now)
    return payload


def prepare_cached(store, now):
    """Publish independently of the smaller Mix tapes preparation deadline.

    Snapshot memoization reuses unchanged inputs while menus fill their batches;
    actual table/setting dependencies and the time window retire that reuse.
    """
    from yt_playlist.core.activity import work_budget, WorkBudgetExceeded
    from yt_playlist.rec import choice_evidence, discover, mode_surfaces, progressive, transient
    try:
        with work_budget(progressive.Budget().fit_seconds), store.read_snapshot() as snapshot:
            reader = snapshot.store

            def calculate():
                leans = transient.facet_leans(reader, now)
                discoveries = discover.pick_discovered_albums(reader, 60, now, facet_leans=leans)
                return {'built_at': now, 'items': prepare(reader, now, discoveries),
                        'context': choice_evidence.freeze_context(reader,
                            choice_evidence.model_context(reader, now, facet_leans=leans))}

            payload = reader.memoized_read(('album-choices', 5, int(now // MAX_AGE_S)), calculate)
            encoded = json.dumps(payload, separators=(',', ':'))
            if not snapshot.publish(lambda: store.rec_proposals.publish_prepared_home(
                    encoded, payload['built_at'], surface=SURFACE),
                    allow_stale_tables=mode_surfaces.RANKING_CHURN_TABLES):
                return None
            return payload
    except WorkBudgetExceeded:
        store.maintenance_diagnostics.record('deferred', reasons=('time_budget',), phase='rec: Albums')
        _LOG.debug('Album preparation deferred; keeping previous albums', exc_info=True)
        return None


def feedback_state(store):
    return json.loads(store.settings.get_setting("album_feedback") or "{}")


def set_feedback(store, browse, kind, now, opportunity_id=None):
    """Explicit release feedback, reversible independently of album saves/track ratings."""
    import uuid
    from yt_playlist.rec import choice_evidence
    if kind not in {"more", "known", "reject", "clear"} or not browse:
        raise ValueError("Choose an album feedback action")
    states = feedback_state(store)
    previous = states.pop(browse, None)
    if previous and previous.get("opportunity_id") and previous.get("action_id"):
        choice_evidence.safe_respond(store, previous["opportunity_id"], "withdraw", now,
            action_id=uuid.uuid4().hex, details={"action_id": previous["action_id"]})
    if kind != "clear":
        action = uuid.uuid4().hex
        states[browse] = {"kind": kind, "at": now, "opportunity_id": opportunity_id, "action_id": action}
        if opportunity_id:
            choice_evidence.safe_respond(store, opportunity_id, "feedback", now, option_id=browse,
                                        action_id=action, details={"kind": kind})
    store.settings.set_setting("album_feedback", json.dumps(states))


def _release_family(title, artist):
    """Conservative edition grouping for explanations, never for track identity."""
    key = recording_title(title)
    credit = recording_title(artist)
    if credit:
        key = re.sub(r'(?<!\w)' + re.escape(credit) + r'(?!\w)', ' ', key)
    key = re.sub(r'\b(?:dj mix|continuous mix|unmixed|mixed|remastered?|deluxe edition)\b', ' ', key)
    return ' '.join(key.split())


def _rationale(album, context, similarities, basis):
    """Explain an actual positive model match, excluding a release's own seed.

    This describes supporting evidence, not a causal explanation of every ranking
    adjustment. Keep it with the prepared option so the held menu is reproducible.
    """
    sources = getattr(context, 'sources', [
        {'kind': 'playlist', 'id': pid} if pid is not None else {'kind': 'history'}
        for pid in context.pids])
    candidates = []
    for index, similarity in enumerate(similarities):
        source = sources[index] if index < len(sources) else {'kind': 'history'}
        own = ((source.get('kind') == 'album' and source.get('browse') == album['browse_id'])
               or (source.get('kind') == 'albums' and source.get('id') == album['browse_id']))
        if source.get('kind') == 'album':
            # A saved or imported record is a taste input, but does not prove
            # that the listener knows it. References need repeat listening.
            if source.get('return_weight', 0) <= 0:
                continue
            artist, separator, title = context.titles[index].partition(' · ')
            own |= bool(separator and normalize(artist) == normalize(album.get('artist'))
                        and _release_family(title, artist)
                        == _release_family(album.get('title'), album.get('artist')))
        if not own and similarity > 0 and context.weights[index] > 0:
            candidates.append((float(similarity), index, source))
    if candidates:
        similarity, index, source = max(candidates, key=lambda row: row[0])
        title = context.titles[index]
        kind = source['kind']
        if kind == 'playlist':
            text = f'A possible fit for your “{title}” playlist.'
        elif kind == 'album':
            text = (f'A possible fit with tracks you keep returning to on “{title}”.'
                    if source['return_coverage'] < 1 else
                    f'A possible fit with your repeat listening to “{title}”.')
        elif kind in {'albums', 'mixes', 'radio'}:
            name = {'albums': 'Long play', 'mixes': 'Mix tapes', 'radio': 'On air'}[kind]
            text = f'A possible fit with the music you’ve chosen in {name}.'
        else:
            text = 'A possible fit with music you’ve played.'
        return {'text': text, 'basis': basis, 'similarity': similarity,
                'context': title, 'source': source}
    # A saved record matching its own seed is not an independent recommendation
    # reason. Unknown and unsupported matches stay absent from the reader view.
    return None


def prepare(store, now, discoveries):
    snapshot = store.rec_model.content_generation()
    _, matrix, index = embedding_vectors.load_content_vectors(store, snapshot=snapshot)
    taste = scoring.content_taste(store, snapshot=snapshot, now=now)
    model = json.loads(snapshot["model"]) if snapshot['model'] else None
    _, collaborative, collab_index = embedding_vectors.load_vectors(store)
    collab_taste = scoring.playlist_taste(store, now=now) if collaborative is not None else None
    albums = store.collection.taste_album_contexts()
    candidates = {a["browse"]: {**a, "browse_id": a["browse"], "classification": "rediscovery"}
                  for a in albums if a["saved"]}
    # Saving a release is useful evidence even before any tracks have been enriched or modeled.
    for album in store.collection.get_saved_albums():
        candidates.setdefault(album['browse'], {**album, 'browse_id': album['browse'],
                                                'keys': [], 'classification': 'rediscovery'})
    for album in discoveries:
        candidates.setdefault(album["browse_id"], {**album, "classification": "discovery"})
    track_genres = store.rec_query.track_genres({key for album in candidates.values()
                                                for key in album.get('keys', [])})
    for album in candidates.values():
        if not album.get('genre'):
            genres = Counter(track_genres[key] for key in album.get('keys', []) if track_genres.get(key))
            common = genres.most_common(2)
            if common and (len(common) == 1 or common[0][1] > common[1][1]):
                album['genre'] = common[0][0]
    familiar_artists = store.rec_query.library_artists()
    for album in candidates.values():
        album['unsaved_classification'] = ('familiar_artist'
            if normalize(album.get('artist') or '') in familiar_artists else 'discovery')
        if album["classification"] == "discovery":
            album["classification"] = album['unsaved_classification']
    feedback = feedback_state(store)
    ranked, features, collab_features = [], [], []
    for album in candidates.values():
        if feedback.get(album["browse_id"], {}).get("kind") == "reject":
            continue
        if feedback.get(album["browse_id"], {}).get("kind") == "known":
            album = {**album, "classification": "known"}
        rows = [index[key] for key in album.get("keys", []) if key in index]
        vector = (matrix[rows].mean(0) if rows else
                  content_model.encode_content(model, album.get("genre"), album.get("year"), {}) if model else None)
        collab_rows = [collab_index[key] for key in album.get('keys', []) if key in collab_index]
        collab_vector = collaborative[collab_rows].mean(0) if collab_rows else None
        def unit(value):
            norm = np.linalg.norm(value) if value is not None else 0
            return value / norm if norm else None
        ranked.append({key: album.get(key) for key in
            ("browse_id", "id", "artist", "title", "year", "genre", "thumbnail", "classification",
             "unsaved_classification", "keys")})
        features.append(unit(vector))
        collab_features.append(unit(collab_vector))
    if not ranked:
        return []
    values, interests, bases, spaces, affinities, rationales = {}, {}, {}, {}, {}, {}
    content_positions = [i for i, feature in enumerate(features) if feature is not None] if taste else []
    collab_positions = [i for i, feature in enumerate(collab_features) if feature is not None] if collab_taste else []
    for positions, feature_rows, context, evidence, lookup, basis in (
            (content_positions, features, taste, matrix, index,
             embedding_vectors.content_space_id(store, snapshot=snapshot)),
            (collab_positions, collab_features, collab_taste, collaborative, collab_index, 'collaborative')):
        if not positions:
            continue
        candidates_matrix = np.asarray([feature_rows[i] for i in positions])
        ids = [ranked[i]['browse_id'] for i in positions]
        candidate_index = {key: i for i, key in enumerate(ids)}
        is_content = basis != 'collaborative'
        scores = scoring._apply_mood(context.allocated_scores(candidates_matrix), store, now,
            candidates_matrix, candidate_index, evidence_vecs=(evidence, lookup),
            content_vecs=(ids, candidates_matrix, candidate_index) if is_content else None,
            content_snapshot=snapshot)
        if is_content:
            posterior, _, modes = layers.now_mode_mix(store, now)
            if posterior and modes and len(modes[0]['centroid']) == candidates_matrix.shape[1]:
                centroids = np.asarray([mode['centroid'] for mode in modes])
                nearest = np.argmax(candidates_matrix @ centroids.T, axis=1)
                scores *= np.asarray([1 + rec_params.get_param(store, 'now_gain')
                                      * posterior.get(modes[mid]['mode_id'], 0) for mid in nearest])
        fit = context.centroids @ candidates_matrix.T
        for local, i in enumerate(positions):
            if i in values:
                weight = rec_params.get_param(store, 'album_collab_weight')
                values[i] = (1 - weight) * values[i] + weight * float(scores[local])
                continue  # Content coordinates still explain and diversify this release.
            values[i], interests[i], bases[i], spaces[i] = float(scores[local]), int(fit[:, local].argmax()), basis, feature_rows[i]
            affinities[i] = float(fit[:, local].max())
            rationales[i] = _rationale(ranked[i], context, fit[:, local], basis)
    metadata = {str(i): {'genre': album.get('genre'), 'decade': decade(album.get('year'), as_str=True),
                          'artist': album.get('artist')} for i, album in enumerate(ranked)}
    mult = scoring.metadata_axis_weights(store, metadata, now)
    adjusted = scoring.axis_adjusted_scores({str(i): value for i, value in values.items()}, mult,
                                           cap=rec_params.get_param(store, 'axis_weight_cap'))
    values = {int(i): value for i, value in adjusted.items()}
    order = sorted(range(len(ranked)), key=lambda i: (i not in values, -values.get(i, 0)))
    companions = [[] for album in ranked]
    if content_positions:
        selected = _companions(store, [ranked[i] for i in content_positions],
            np.asarray([features[i] for i in content_positions]), matrix, index)
        for i, companion in zip(content_positions, selected):
            companions[i] = companion
    # Reserve frontier capacity for discovery before saved releases can crowd it out.
    # Serving can then vary the requested balance without fitting a model on a request.
    unseen = [i for i in order if ranked[i]['classification'] in {'discovery', 'familiar_artist'}]
    unseen_set = set(unseen)
    saved = [i for i in order if i not in unseen_set]

    def distinct_first(bucket):
        artists, distinct, remainder = set(), [], []
        for i in bucket:
            artist = normalize(ranked[i].get('artist') or ranked[i]['browse_id'])
            (remainder if artist in artists else distinct).append(i)
            artists.add(artist)
        return (distinct + remainder)[:12]

    frontier = set(distinct_first(unseen) + distinct_first(saved))
    for i in order:
        if len(frontier) >= 24:
            break
        frontier.add(i)
    return [{**ranked[i], "score": values.get(i), "feature": spaces[i].tolist() if i in spaces else None,
             'affinity': affinities.get(i),
             'rationale': rationales.get(i),
             "companion": companions[i],
             "interest": interests.get(i), "basis": bases.get(i),
             'prediction_status': 'scored' if i in values else 'unscored'}
            for i in order if i in frontier]


def menu(items, controls, epoch=0, genres=(), *, exclude=()):
    """Allocate an explicit observational menu from already scored options.

    Discovery and fit are distinct dimensions. A missing discovery bucket stays a
    recorded coverage gap. Positions rotate; this is not randomized causal exposure.
    """
    from yt_playlist.util import genre_map
    candidates = [item for item in items if not genres or genre_map.family(item.get('genre') or '') in genres]
    excluded = set(exclude)
    novel = lambda item: item.get('classification') in {'discovery', 'familiar_artist'}
    novelty = controls['novelty']
    target = min(4, max(0, int(4 * novelty + .5)))
    ranks = {item['browse_id']: 1 - index / max(1, len(candidates) - 1)
             for index, item in enumerate(candidates)}
    chosen, artists = [], set()
    positions = {item['browse_id']: i for i, item in enumerate(candidates)}

    def utility(item):
        rank = ranks[item['browse_id']]
        reach = .7 * novelty
        value = (1 - reach) * rank + reach * (1 - rank) * .6
        # Another set rotates within a small, model-ranked frontier rather than
        # repeatedly showing the identical first four records.
        value += .12 * ((epoch + positions[item['browse_id']]) % 6) / 5
        return value

    requested = [True] * target + [False] * (4 - target)
    # Reserve scarce discovery artists first. Rotate display positions afterwards;
    # a familiar fallback must not consume an artist needed by a discovery slot.
    for discovery in requested:
        available = [item for item in candidates if normalize(item.get('artist') or item['browse_id']) not in artists
                     and item['browse_id'] not in {row['browse_id'] for row in chosen}]
        bucket = [item for item in available if novel(item) == discovery]
        if not bucket:
            bucket = available
        if not bucket:
            break
        # Refresh prefers a fresh record within each requested bucket. Preserve
        # explicit novelty/genre constraints when a sparse pool requires repeats.
        picked = max(bucket, key=lambda item: (item['browse_id'] not in excluded, utility(item)))
        role = 'discovery' if discovery else 'familiar'
        chosen.append({**picked, 'hypothesis': {'requested': role,
            'actual': 'discovery' if novel(picked) else 'familiar',
            'interest': picked.get('interest'), 'affinity': picked.get('affinity'),
            'model_rank': positions[picked['browse_id']] + 1,
            'selection_utility': utility(picked),
            'fresh': picked['browse_id'] not in excluded,
            'novelty': novelty, 'reach': .7 * novelty}})
        artists.add(normalize(picked.get('artist') or picked['browse_id']))
    offset = epoch % len(chosen) if chosen else 0
    chosen = chosen[offset:] + chosen[:offset]
    return chosen, {'version': 3, 'kind': 'observational_album_menu', 'controls': controls,
        'presentation': 'equal_album_stories_v2',
        'genres': list(genres), 'eligible': len(candidates),
        'previous_selection': sorted(excluded),
        'discovery_available': sum(novel(item) for item in candidates),
        'discovery_requested': target, 'discovery_offered': sum(novel(item) for item in chosen),
        'coverage_gap': max(0, target - sum(novel(item) for item in chosen)),
        'eligible_options': [{'id': item['browse_id'], 'score': item.get('score'),
                             'interest': item.get('interest'), 'basis': item.get('basis'),
                             'classification': item.get('classification')} for item in candidates],
        'artist_cap': 1, 'position_rotation': epoch % 4, 'causal_comparison': False}


def _companions(store, albums, features, matrix, index):
    """Bounded worker-only album anchors over existing prepared mix candidates."""
    from yt_playlist.rec import home_cards, rec_params
    bundles = store.rec_proposals.get_proposals("mode_bundles") or {}
    pool = {track["key"]: track for tracks in bundles.get("all", {}).values()
            for track in tracks if track.get("key") in index and track.get("video_id")}
    keys = list(pool)
    if not keys:
        return [[] for album in albums]
    vectors = matrix[[index[key] for key in keys]]
    vectors = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)
    ranks = np.argsort(-(features @ vectors.T), axis=1, kind="stable")
    cap = rec_params.get_param(store, "modes_artist_cap")
    results = []
    for album, order in zip(albums, ranks):
        own, artists, chosen = set(album.get("keys") or []), {}, []
        for position in order:
            track = pool[keys[position]]
            artist = (track.get("artist") or "").casefold()
            if track["key"] in own or artists.get(artist, 0) >= cap:
                continue
            chosen.append(track)
            artists[artist] = artists.get(artist, 0) + 1
            if len(chosen) == home_cards.PROTO_SIZE:
                break
        results.append(chosen)
    return results


def eligible(store, items, now):
    """Recheck current explicit constraints without synchronous model work."""
    from yt_playlist.util import genre_map
    muted = store.rec_model.muted_artists()
    weights = store.rec_model.get_weights(now=now)
    feedback = feedback_state(store)
    saved = store.collection.saved_album_ids(browse_ids=[album['browse_id'] for album in items])
    result = []
    for album in items:
        kind = feedback.get(album["browse_id"], {}).get("kind")
        if kind == "reject":
            continue
        if album.get("artist") in muted:
            continue
        genre = album.get("genre")
        if genre and (weights.get("genre:" + genre_map.family(genre), 1) == 0
                      or weights.get("genre:" + (genre_map.subgenre(genre) or ""), 1) == 0):
            continue
        # Reconcile mutable collection/feedback state over the bounded prepared
        # frontier. Preserve the scored object and already-issued evidence unchanged.
        unsaved = album.get('unsaved_classification') or (
            'familiar_artist' if album.get('classification') == 'familiar_artist' else 'discovery')
        classification = ('known' if kind == 'known' else 'rediscovery'
                          if album['browse_id'] in saved else unsaved)
        result.append({**album, 'classification': classification})
    return result
