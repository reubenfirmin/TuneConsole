"""Acquire bounded local recommendation facts before detached surface ranking."""
from yt_playlist.rec import surface_values as surfaces
from yt_playlist.core.activity import check_stopping


class SurfaceQueries:
    def __init__(self, query, candidates, model, genres, *, parameter, taste, vectors, score, content):
        self.query, self.candidates, self.model, self.genres = query, candidates, model, genres
        self.parameter, self.taste, self.vectors, self.score, self.content = parameter, taste, vectors, score, content

    def wheelhouse(self, now, limit=24, include_traces=False):
        pool = limit * self.parameter('candidate_pool_factor')
        sims, traces, taste, vectors, index = None, None, None, None, None
        if self.model.rec_vectors_count():
            taste = self.taste(now)
            keys, vectors, index = self.vectors()
            if taste and vectors is not None:
                scored = self.score(taste, keys, vectors, index, now, include_traces=include_traces)
                sims, traces = scored if include_traces else (scored, None)
        if sims is not None:
            seeds = set(self.query.top_played_keys(limit=8))
            top = surfaces.neighborhood_keys(sims, seeds, pool)
            metadata = self.query.tracks_by_keys(top)
            rows = [{'key': key, 'plays': 0, **metadata[key]} for key in top if key in metadata]
            sources = [(rows, lambda row: 'In your taste neighbourhood', 'neighbourhood')]
        else:
            sources = [(self.candidates.more_like_rotation(limit=pool),
                        lambda row: surfaces._rotation_reason(row['shared_playlists']), 'rotation')]
        sources.append((self.candidates.deep_cuts(limit=pool),
                        lambda row: f"A deep cut from {row['artist']}, who you play a lot", 'deep_cut'))
        weights = self.model.get_weights(now=now, revert_halflife_d=self.parameter('weight_revert_halflife_d'))
        return surfaces.wheelhouse_values(sources, sims, weights, self.model.suppressed_keys('for_you', now),
            self.model.muted_artists(), limit, traces=traces, taste=taste, vectors=vectors, index=index,
            include_traces=include_traces)

    def comfort(self, now, limit=24):
        recency = self.parameter('comfort_recency_full_days')
        rows = self.candidates.comfort_candidates(now, min_plays=self.parameter('comfort_min_plays'),
            recency_full_days=recency, limit=limit * self.parameter('candidate_pool_factor'))
        return surfaces.comfort_values(rows, self.model.suppressed_keys('for_you', now),
                                       self.model.muted_artists(), limit)

    def catalog(self, now, limit=24):
        if limit <= 0:
            return []
        plays, taste = self.query.play_counts(), self.taste(now)
        keys, vectors, index = self.vectors()
        scores = self.score(taste, keys, vectors, index, now) if taste and vectors is not None else {}
        collab = surfaces._catalog_scores(scores, plays, all_keys=keys) if scores else {}
        content = self.content(plays, now)
        owned = self.query.library_keys()
        order, missing = surfaces.catalog_order(collab, content, owned, plays)
        weights = self.model.get_weights(now=now, revert_halflife_d=self.parameter('weight_revert_halflife_d'))
        suppressed = self.model.suppressed_keys('for_you', now)
        muted = self.model.muted_artists()
        batch_size = max(1, int(limit * self.parameter('candidate_pool_factor')))
        results = []
        for start in range(0, len(order), batch_size):
            check_stopping()
            batch = [key for key in order[start:start + batch_size] if key not in suppressed]
            if not batch:
                continue
            genres = self.query.track_genres(batch)
            axes = self.genres.feature_axes(genres.values())
            eligible = surfaces.catalog_eligible(batch, (), genres, axes, weights)
            if not eligible:
                continue
            metadata = self.query.tracks_by_keys(eligible)
            results.extend(surfaces.catalog_values(eligible, metadata, plays, muted, collab,
                                                   content, missing, limit - len(results)))
            if len(results) == limit:
                break
        return results
