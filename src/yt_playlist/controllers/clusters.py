"""Cluster acquisition, exposure and complete generated-save workflows."""
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.rec.cluster_explain import connection_reasons
from yt_playlist.rec.cluster_selection import ring
from yt_playlist.util.navigation import playlist_url


class ClusterQuery:
    def __init__(self, query, discovery, genres, evidence, *, rank_candidates, geometry):
        self.query, self.discovery, self.genres, self.evidence = query, discovery, genres, evidence
        self.rank_candidates, self.geometry = rank_candidates, geometry

    def expansion(self, body):
        pos, neg, exclude = (body.get(key) or [] for key in ('pos_keys', 'neg_keys', 'exclude'))
        tokens = body.get('allow_genres') or body.get('allow_families') or []
        allow = self.query.keys_in_genre_selection(tokens) if tokens else None
        seeds = body.get('central_seeds') or []
        groups = [s['keys'] for s in seeds if s.get('keys')]
        artists = [s for s in seeds if s.get('kind') == 'artist' and s.get('keys')]
        artist_keys = [set(s['keys']) for s in artists]
        songs = {key for s in seeds if s.get('kind') == 'song' for key in s.get('keys', [])}
        preferred = {key for s in artists for key in s['keys']} - songs
        size = int(body.get('k') or 6)
        include_new = bool(body.get('include_new'))
        neighbors = self.rank_candidates(pos_keys=pos, neg_keys=neg, exclude=exclude,
            topn=len(preferred) + max(size * 4, size + 12), allow=allow, include_new=include_new,
            seed_groups=groups, candidate_seed_keys=preferred, artist_groups=artist_keys)
        basis = body.get('count_keys')
        if basis is None:
            basis = list(set(exclude) - set(neg))
        existing = self.query.tracks_by_keys(basis)
        keys = [key for key, _ in neighbors]
        metadata = self.query.tracks_by_keys(keys)
        discovered = self.discovery.playable_discoveries_by_keys(keys) if include_new else {}
        genres = self.query.track_genres(keys)
        axes = self.genres.feature_axes([*genres.values(), *(m.get('genre') for m in discovered.values())])
        check_stopping()
        return ring(body, neighbors, metadata, discovered, genres, axes, existing)

    def explanation(self, body):
        key = (body.get('key') or '').strip()
        path = [k for k in (body.get('path_keys') or []) if k and k != key]
        if not key:
            return {'key': '', 'title': '', 'artist': '', 'headline': '',
                    'reasons': [], 'score': None, 'match_pct': None}, None
        reasons = connection_reasons(self.evidence.connection(key, path))
        geometry = self.geometry(key, path)
        if not reasons and geometry['bridge']:
            bridge = self.query.tracks_by_keys([geometry['bridge']]).get(geometry['bridge'])
            if bridge:
                reasons.append({'kind': 'bridge', 'text':
                    f"Linked through “{bridge['title']}” by {bridge['artist']}, a song similar to both."})
        metadata = self.query.tracks_by_keys([key]).get(key, {})
        score = geometry['score']
        return {'key': key, 'title': metadata.get('title', ''), 'artist': metadata.get('artist', ''),
            'headline': reasons[0]['text'] if reasons else 'Similar to the music you chose.',
            'reasons': reasons, 'score': round(score, 4) if score is not None else None,
            'match_pct': round(max(0., score) * 100) if score is not None else None}, None


class Clusters:
    def __init__(self, query, discovery, recipes, evidence, model, *, scans, expand, explain,
                 recipe, generated, record, now, logger):
        self.query, self.discovery, self.recipes, self.evidence, self.model = query, discovery, recipes, evidence, model
        self.scans, self.expand, self.explain = scans, expand, explain
        self.recipe, self.generated, self.record, self.now, self.logger = recipe, generated, record, now, logger

    def page(self):
        from yt_playlist.rec import journeys
        return {'have_model': self.model.rec_vectors_count() > 0,
                'journeys': [{'key': k, 'label': journeys.JOURNEY_LABELS[k], 'hint': journeys.JOURNEY_HINTS[k]}
                             for k in journeys.JOURNEYS]}

    def search(self, query):
        return self.evidence.seed_matches(query, limit=6)

    def canvas(self, playlist):
        return self.recipes.get_cluster_canvas(playlist)

    def genres(self):
        return {'families': self.query.library_genre_families(), 'genres': self.query.library_genres()}

    async def expansion(self, body):
        return await self.scans.run(self.expand, body, self._offered)

    async def explanation(self, body):
        return await self.scans.run(self.explain, body)

    def _offered(self, payload, offered, body):
        now = self.now()
        if offered:
            self.discovery.mark_offered('track', offered, now)
        self.record('clusters', payload['ring'], now, lane='ring', ranker='cluster-expand',
            provenance={'positive_seeds': body.get('pos_keys') or [], 'negative_seeds': body.get('neg_keys') or [],
                        'genre_selection': body.get('allow_genres') or body.get('allow_families') or []})

    def admit_save(self):
        return self.generated().admit()

    def _playable_tracks(self, keys):
        library = self.query.tracks_by_keys(keys)
        discoveries = self.discovery.playable_discoveries_by_keys([key for key in keys if key not in library])
        return {**discoveries, **library}

    def playable_uploads(self, keys):
        return {key: row['video_id'] for key, row in self._playable_tracks(keys).items()}

    def save(self, name, keep, central, labels, families, journey, canvas, intent):
        keep = list(dict.fromkeys([*keep, *central]))
        metadata = self._playable_tracks(keep)
        if not metadata:
            return {'name': name, 'error': 'Keep at least one track to create a playlist.'}
        recipe, order = self.recipe([key for key in keep if key in metadata], labels, families, journey=journey)
        tracks = [{field: metadata[key][field] for field in ('video_id', 'title', 'artist', 'album', 'thumbnail')}
                  for key in order]
        result = {'name': name}
        try:
            saved = self.generated().create(name, tracks, recipe=recipe, navigate=True, intent=intent, canvas=canvas)
            result.update(ytm=saved['new_ytm'], pid=saved['pid'], added=saved['added'])
            result['destination_url'] = saved.get('playlist_url') or playlist_url(saved['pid'])
            if saved['status'] == 'rejected':
                result['error'] = 'No songs were added to this playlist.'
        except PlaylistAddUncertain as exc:
            result.update(error=str(exc), partial_ytm=exc.playlist_id)
        except ActionInputError as exc:
            result['error'] = str(exc)
        except Exception:
            self.logger.exception('save cluster %r failed', name)
            result['error'] = 'YouTube returned an unexpected response.'
        return result
