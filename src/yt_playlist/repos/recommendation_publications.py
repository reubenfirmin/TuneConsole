"""Small committed heads, bounded rank pages and directly owned opaque chunks."""
from yt_playlist.core import recommendation_outputs, recommendation_pools, recommendation_revalidations
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core import listening_subject_reads
from yt_playlist.core.preparation import listening_views
from yt_playlist.library.rating_policy import LIKED_EXISTS
from .base import Repo, lookup_read, snapshot_read, synchronized
from . import recommendation_publication as publication


def decode_display(chunks, ranks, surface):
    """Inflate detached immutable output references after releasing SQL readers."""
    wanted, result = set(ranks), {}
    for row in chunks:
        value = recommendation_outputs.read(row['filename'], row['content_digest'], row['byte_size'],
                                             surface=surface, chunk_key=row['chunk_key'])
        for rank, owner, explanation in value['rows']:
            if rank in wanted:
                result[rank] = {**explanation, 'owner': owner}
    return result


class RecommendationPublicationsRepo(Repo):
    @snapshot_read
    def prepare_clicked_pool(self, surface, value, at, **options):
        from yt_playlist.core import library_pool_state
        return library_pool_state.prepare(self.conn, surface, value, at, **options)

    @synchronized
    def remember_clicked_pool_parameters(self, generation, parameters, at):
        from yt_playlist.core import library_pool_state
        with self.conn.atomic():
            library_pool_state.parameters(self.conn, generation, parameters, at)

    def prepare_bundles(self, bundle, fresh, *, previous, **options):
        """Resolve canonical owners on the pinned ranking and prior-output views."""
        from . import bundle_storage
        if not self._store._read_only or not previous._store._read_only:
            raise ValueError('Bundle preparation requires private readers')
        with self._lock, previous._lock:
            return bundle_storage.prepare(self.conn, bundle, fresh, reuse_existing=True,
                                          reuse_conn=previous.conn, **options)

    @snapshot_read
    def captured_policies(self, versions):
        from . import policy_storage
        return policy_storage.captured(self.conn, versions)

    def claim(self, plan, *, owner=None, policies=(), bases=()):
        return publication.claim(self, plan, owner=owner, policies=policies, bases=bases)

    def stage(self, plan, build, *, still_current=None):
        return publication.stage(self, plan, build, still_current=still_current)

    @synchronized
    def publish(self, build, at, *, retire_surfaces=()):
        result = publication.finish(self.conn, build, at)
        if retire_surfaces:
            self.conn.execute('DELETE FROM recommendation_heads WHERE surface IN (' +
                              ','.join('?' for _ in retire_surfaces) + ')', tuple(retire_surfaces))
        return result

    def cleanup(self):
        return publication.cleanup(self)

    @snapshot_read
    def retirement_pending(self):
        return publication.retirement_pending(self.conn)

    def checkpoint_retirement(self):
        """Best-effort WAL copy after a bounded turn has released its writer."""
        from yt_playlist.core.db_checkpoint import checkpoint_wal
        if self._store._db_path is not None:
            checkpoint_wal(self._store._db_path)

    @lookup_read
    def head(self, surface):
        row = self.conn.execute('SELECT g.* FROM recommendation_heads h JOIN recommendation_generations g '
                                'ON g.id=h.generation_id WHERE h.surface=?', (surface,)).fetchone()
        return dict(row) if row else None

    @snapshot_read
    def heads(self):
        rows = self.conn.execute('SELECT h.surface,g.id,g.output_digest FROM recommendation_heads h '
            'JOIN recommendation_generations g ON g.id=h.generation_id ORDER BY h.surface LIMIT 129').fetchall()
        if len(rows) > 128:
            raise WorkBudgetExceeded('Recommendation head inventory exceeds its read budget')
        return [dict(row) for row in rows]

    def _generation(self, surface, generation):
        if generation is None:
            row = self.conn.execute('SELECT generation_id FROM recommendation_heads WHERE surface=?', (surface,)).fetchone()
        else:
            row = self.conn.execute('SELECT s.generation_id FROM recommendation_generation_surfaces s '
                'JOIN recommendation_generations g ON g.id=s.generation_id '
                "WHERE s.surface=? AND s.generation_id=? AND g.status='published'", (surface, generation)).fetchone()
        return row[0] if row else None

    @lookup_read
    def pool(self, surface, mode_key, purpose, *, generation=None):
        generation = self._generation(surface, generation)
        if generation is None:
            return None
        revalidated = getattr(self._store, '_recommendation_revalidations', False)
        if not revalidated:
            # The regular-schema worker can add this owner after the Store binds
            # its repositories. Adopt it on the first subsequent read as well.
            revalidated = recommendation_revalidations.available(self.conn)
            if revalidated:
                self._store._recommendation_revalidations = True
        projection = ',v.policy_version AS revalidated_policy' if revalidated else ''
        joining = ('LEFT JOIN recommendation_pool_revalidations v ON v.generation_id=m.generation_id '
                   'AND v.surface=m.surface AND v.mode_key=m.mode_key AND v.purpose=m.purpose AND v.pool_id=m.pool_id '
                   if revalidated else '')
        row = self.conn.execute('SELECT p.*' + projection + ' FROM recommendation_surface_pools m '
            'JOIN recommendation_pools p ON p.id=m.pool_id ' + joining + 'WHERE m.generation_id=? AND m.surface=? '
            'AND m.mode_key=? AND m.purpose=?', (generation, surface, mode_key, purpose)).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result.get('revalidated_policy') is not None:
            result['source_policy_version'] = result['policy_version']
            result['policy_version'] = result['revalidated_policy']
        return result

    @snapshot_read
    def page(self, surface, mode_key, purpose, *, generation=None, after=-1, limit=128, band=None, genres=None,
             group_version=None):
        pool = self.pool(surface, mode_key, purpose, generation=generation)
        if pool is None:
            return []
        rows = recommendation_pools.page(self.conn, pool['id'], after=after, limit=limit, band=band, genres=genres,
                                         group_version=group_version)
        if rows:
            marks = ','.join('?' for _ in rows)
            keys = dict(self.conn.execute(f'SELECT id,identity_key FROM recordings WHERE id IN ({marks})',
                                          [row['recording_id'] for row in rows]))
            rows = [{**row, 'key': keys[row['recording_id']], 'pool_policy': pool['policy_version']} for row in rows]
        return rows

    @snapshot_read
    def output(self, surface, chunk_key, *, generation=None):
        generation = self._generation(surface, generation)
        if generation is None:
            return None
        row = self.conn.execute('SELECT filename,content_digest,byte_size FROM prepared_outputs '
            'WHERE generation_id=? AND surface=? AND chunk_key=?', (generation, surface, chunk_key)).fetchone()
        if row is None:
            return None
        return recommendation_outputs.read(*row, surface=surface, chunk_key=chunk_key)

    @snapshot_read
    def display(self, surface, mode_key, purpose, ranks, *, generation=None):
        """Read only explanation chunks covering this bounded candidate lookahead."""
        return decode_display(self.display_references(surface, mode_key, purpose, ranks,
                              generation=generation), ranks, surface)

    @snapshot_read
    def display_references(self, surface, mode_key, purpose, ranks, *, generation=None):
        """Capture bounded file owners without reading or decoding their content."""
        if (not isinstance(ranks, (list, tuple)) or len(ranks) > 512
                or any(type(rank) is not int or rank < 0 for rank in ranks)):
            raise ValueError('Invalid bounded display ranks')
        if not ranks:
            return []
        generation = self._generation(surface, generation)
        pool = self.pool(surface, mode_key, purpose, generation=generation) if generation is not None else None
        if pool is None:
            return []
        wanted = set(ranks)
        # A predecessor seek per requested rank uses the composite rank index;
        # sparse explanation ranges do not cause a scan over unrelated chunks.
        values = ','.join('(?)' for _ in wanted)
        chunks = self.conn.execute('WITH wanted(rank) AS (VALUES ' + values + ') '
            'SELECT DISTINCT o.chunk_key,o.filename,o.content_digest,o.byte_size FROM wanted w '
            'JOIN prepared_outputs o ON o.id=(SELECT id FROM prepared_outputs '
            'WHERE generation_id=? AND surface=? AND pool_id=? AND first_rank<=w.rank '
            'ORDER BY first_rank DESC LIMIT 1) WHERE o.last_rank>=w.rank',
            (*wanted, generation, surface, pool['id'])).fetchall()
        if sum(row['byte_size'] for row in chunks) > 4 * recommendation_outputs.MAX_BYTES:
            raise WorkBudgetExceeded('Prepared display lookahead exceeds its read budget')
        return [dict(row) for row in chunks]


    @lookup_read
    def framing(self, surface, *, generation=None):
        generation = self._generation(surface, generation)
        if generation is None:
            return None
        row = self.conn.execute('SELECT * FROM recommendation_bundle_framing WHERE generation_id=?',
                                (generation,)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def selections(self, surface, purpose, *, generation=None):
        """List small mode/genre headers without materializing pool members."""
        generation = self._generation(surface, generation)
        if generation is None:
            return []
        rows = self.conn.execute('SELECT m.mode_key,p.id,p.eligible_count FROM recommendation_surface_pools m '
            'JOIN recommendation_pools p ON p.id=m.pool_id WHERE m.generation_id=? AND m.surface=? '
            'AND m.purpose=? ORDER BY m.mode_key LIMIT 513', (generation, surface, purpose)).fetchall()
        if len(rows) > 512:
            raise WorkBudgetExceeded('Recommendation selection headers exceed their read budget')
        return [dict(row) for row in rows]

    @snapshot_read
    def recording_metadata_page(self, surface, *, generation=None, after=0, limit=128, mode_key=None):
        """Current metadata for distinct selected recordings, without pool copies.

        The recording index drives the keyset seek. The reverse candidate and
        selection indexes test membership without collecting a Python key set.
        Explanation files, scores and musical histories are not needed here.
        """
        if (type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 512
                or mode_key is not None and not isinstance(mode_key, str)):
            raise ValueError('Invalid bounded recommendation metadata page')
        generation = self._generation(surface, generation)
        if generation is None:
            return {'generation': None, 'after': after, 'exhausted': True, 'tracks': []}
        mode_clause = ' AND m.mode_key=?' if mode_key is not None else ''
        rows = self.conn.execute('SELECT r.id,r.identity_key FROM recordings r WHERE r.id>? AND EXISTS('
            'SELECT 1 FROM recommendation_candidates c JOIN recommendation_surface_pools m ON m.pool_id=c.pool_id '
            'WHERE c.recording_id=r.id AND m.generation_id=? AND m.surface=?' + mode_clause + ') '
            'ORDER BY r.id LIMIT ?', (after, generation, surface,
                                     *((mode_key,) if mode_key is not None else ()), limit)).fetchall()
        keys = [row['identity_key'] for row in rows]
        owned = self._store.rec_query.group_candidates(keys)
        discovered = self._store.discovery.discovered_tracks_by_keys(keys, available_only=True)
        tracks = []
        for row in rows:
            key = row['identity_key']
            current = owned.get(key) or discovered.get(key)
            if current and current.get('video_id'):
                tracks.append({**current, 'key': key, 'recording_id': row['id'],
                               'serving_context': {'recommendation_generation_id': generation}})
        return {'generation': generation, 'after': rows[-1]['id'] if rows else after,
                'exhausted': len(rows) < limit, 'tracks': tracks}

    @snapshot_read
    def ranks(self, surface, mode_key, purpose, keys, *, generation=None):
        """Resolve a bounded lookahead against its complete PPR/score ranking."""
        if (not isinstance(keys, (list, tuple)) or len(keys) > 512
                or any(not isinstance(key, str) or not key for key in keys)):
            raise ValueError('Invalid bounded recommendation rank lookup')
        pool = self.pool(surface, mode_key, purpose, generation=generation)
        if pool is None or not keys:
            return {}
        marks = ','.join('?' for _ in keys)
        return {row['key']: dict(row) for row in self.conn.execute(
            'SELECT r.identity_key AS key,c.rank,c.score FROM recordings r '
            'JOIN recommendation_candidates c ON c.recording_id=r.id '
            f'WHERE c.pool_id=? AND r.identity_key IN ({marks})', (pool['id'], *keys))}

    @snapshot_read
    def band_page(self, surface, mode_key, purpose, *, generation=None, after=None, limit=128, band=None):
        """Traverse band/genre headings without restoring their member arrays."""
        if (type(limit) is not int or not 1 <= limit <= 512 or after is not None and
                (not isinstance(after, (list, tuple)) or len(after) != 2 or type(after[0]) is not int
                 or not 0 <= after[0] <= 50 or not isinstance(after[1], str))):
            raise ValueError('Invalid bounded radio band page')
        if band is not None and (type(band) is not int or not 0 <= band <= 50):
            raise ValueError('Invalid radio intensity band')
        pool = self.pool(surface, mode_key, purpose, generation=generation)
        if pool is None:
            return []
        predicate = ' AND (intensity_band,genre_key)>(?,?)' if after is not None else ''
        predicate += ' AND intensity_band=?' if band is not None else ''
        return [dict(row) for row in self.conn.execute(
            'SELECT DISTINCT intensity_band,genre_key FROM recommendation_radio_bands '
            'WHERE pool_id=?' + predicate + ' ORDER BY intensity_band,genre_key LIMIT ?',
            (pool['id'], *(after or ()), *((band,) if band is not None else ()), limit))]

    @snapshot_read
    def intensity_bands(self, surface, mode_key, purpose, *, generation=None):
        pool = self.pool(surface, mode_key, purpose, generation=generation)
        if pool is None:
            return []
        return [row[0] for row in self.conn.execute('SELECT DISTINCT intensity_band '
            'FROM recommendation_radio_bands WHERE pool_id=? ORDER BY intensity_band LIMIT 51', (pool['id'],))]

    @snapshot_read
    def candidate_page(self, surface, mode_key, purpose, *, generation=None, after=-1, limit=128,
                       band=None, genres=None, group_version=None, include_display=True):
        """Hydrate one canonical rank page through the existing catalog owners.

        Source uploads, metadata corrections and library/discovery membership
        resolve at read time. The cursor advances past unavailable candidates so
        callers can request another bounded page without losing ranked coverage.
        """
        return self.candidate_pages(surface, [(mode_key, purpose, after)], generation=generation,
            limit=limit, band=band, genres=genres, group_version=group_version,
            include_display=include_display)[0]

    @snapshot_read
    def candidate_pages(self, surface, selections, *, generation=None, limit=128,
                        band=None, genres=None, group_version=None, include_display=True):
        """Hydrate bounded rank pages against one shared, current catalog read."""
        if len(selections) > 16:
            raise ValueError('Candidate batch exceeds 16 pages')
        generation = self._generation(surface, generation)
        pages = []
        for mode_key, purpose, after in selections:
            pool = self.pool(surface, mode_key, purpose, generation=generation) if generation is not None else None
            rows = (self.page(surface, mode_key, purpose, generation=generation, after=after,
                             limit=limit, band=band, genres=genres, group_version=group_version)
                    if pool else [])
            cursor = rows[-1]['rank'] if rows else after
            result = {'generation': generation, 'after': cursor,
                'exhausted': not pool or len(rows) < limit or cursor >= pool['eligible_count'] - 1, 'tracks': []}
            pages.append((mode_key, purpose, pool, rows, result))
        self._hydrate_pages(surface, generation, pages, include_display)
        return [page[-1] for page in pages]

    def _candidate_play_counts(self, keys):
        """Count only requested subjects with the canonical evidence policy.

        Filtering the aggregate listening_day_counts view happens after its
        whole-history eligibility calculation. Materialize the bounded subject
        inputs first so the correlated eligibility probes can index their keys.
        Both sides of those probes refer to the same recording, so unrelated
        subjects cannot affect the result.
        """
        result = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            marks = ','.join('?' for _ in batch)
            if not listening_subject_reads.native(self.conn):
                result.update(self.conn.execute(
                    f'SELECT identity_key,plays FROM listening_day_counts WHERE identity_key IN ({marks})', batch))
                continue
            names = {name: 'radio_' + name for name in
                     ('play_event_subjects', 'listening_observation_subjects',
                      'eligible_listening_evidence', 'listening_day_counts')}
            names.update(sources='play_event_sources', calendar='play_event_calendar')
            inputs = [f'{names[name]} AS MATERIALIZED (SELECT * FROM {name} WHERE identity_key IN ({marks}))'
                      for name in ('play_event_subjects', 'listening_observation_subjects')]
            inputs.extend(f'{names[name]} AS ({query})'
                          for name, query in listening_views.definitions(lambda name: names.get(name, name))
                          if name in ('eligible_listening_evidence', 'listening_day_counts'))
            result.update(self.conn.execute('WITH ' + ','.join(inputs) +
                ' SELECT identity_key,plays FROM ' + names['listening_day_counts'], (*batch, *batch)))
        return result

    def _candidate_facts(self, keys):
        """Current catalog facts shared by overlapping Radio candidate pages.

        The optional cache belongs to one private planning snapshot. Pool ranks,
        explanations and serving attribution are assembled separately per page.
        """
        store = self._store
        cache = getattr(store, '_radio_candidate_facts', None)
        missing = [key for key in keys if cache is None or key not in cache]
        owned = store.rec_query.group_candidates(missing) if missing else {}
        discovered = store.discovery.candidate_tracks_by_keys(missing) if missing else {}
        videos = list(dict.fromkeys(current['video_id'] for key in missing
                      if (current := owned.get(key) or discovered.get(key)) and current.get('video_id')))
        details = {}
        if videos:
            marks = ','.join('?' for _ in videos)
            details = {row['video_id']: dict(row) for row in self.conn.execute(
                'SELECT t.video_id,t.album_browse_id,t.duration_s AS duration,t.bpm,t.energy,t.danceability,'
                f't.available,{LIKED_EXISTS} AS liked FROM catalog_tracks t WHERE t.video_id IN ({marks})', videos)}
        years = store.modes.years_for(missing) if missing else {}
        plays = self._candidate_play_counts(missing)
        facts = {key: cache[key] for key in keys if cache is not None and key in cache}
        for key in missing:
            current = owned.get(key) or discovered.get(key)
            track = None
            if current and current.get('video_id'):
                year = years.get(key) if key in owned else current.get('year')
                if isinstance(year, str):
                    year = int(year[:4]) if len(year) >= 4 and year[:4].isascii() and year[:4].isdigit() else None
                track = {name: current.get(name) for name in ('title', 'artist', 'album', 'video_id', 'thumbnail', 'genre')}
                track.update(details.get(current['video_id'], {}))
                track.update(key=key, year=year, plays=plays.get(key, 0), _radio_fresh=key not in owned)
            facts[key] = track
            if cache is not None:
                cache[key] = track
                if len(cache) > 8192:
                    cache.popitem(last=False)
        return facts

    def _hydrate_pages(self, surface, generation, pages, include_display):
        keys = list(dict.fromkeys(row['key'] for _, _, _, rows, _ in pages for row in rows))
        if not keys:
            return
        facts = self._candidate_facts(keys)
        for mode_key, purpose, pool, rows, result in pages:
            display = (self.display(surface, mode_key, purpose, [row['rank'] for row in rows], generation=generation)
                       if include_display else {})
            for row in rows:
                key = row['key']
                current = facts.get(key)
                if current is None:
                    continue
                explanation = display.get(row['rank'], {})
                track = dict(current)
                track.update(key=key, recording_id=row['recording_id'], pool_rank=row['rank'], score=row['score'],
                    pool_policy=row['pool_policy'],
                    reason=explanation.get('reason', ''), lane=explanation.get('lane', purpose),
                    serving_context={'recommendation_generation_id': generation, 'recommendation_pool_id': pool['id'],
                                     'recommendation_pool_rank': row['rank']})
                result['tracks'].append(track)

    @snapshot_read
    def selected_candidates(self, surface, selections, *, generation=None):
        """Hydrate exact bounded selections from the same committed rank pools."""
        if len(selections) > 16 or any(len(keys) > 512 for _, _, keys in selections):
            raise ValueError('Candidate selection exceeds its read budget')
        generation = self._generation(surface, generation)
        pages = []
        for mode, purpose, keys in selections:
            pool = self.pool(surface, mode, purpose, generation=generation) if generation is not None else None
            rows = []
            if pool and keys:
                marks = ','.join('?' for _ in keys)
                rows = [dict(row, pool_policy=pool['policy_version']) for row in self.conn.execute(
                    'SELECT r.identity_key AS key,c.recording_id,c.rank,c.score FROM recommendation_candidates c '
                    'JOIN recordings r ON r.id=c.recording_id '
                    f'WHERE c.pool_id=? AND r.identity_key IN ({marks}) ORDER BY c.rank', (pool['id'], *keys))]
            pages.append((mode, purpose, pool, rows, {'tracks': []}))
        self._hydrate_pages(surface, generation, pages, True)
        return [page[-1]['tracks'] for page in pages]

    @synchronized
    def retain(self, kind, external_key, generation_id):
        return publication.retain(self.conn, kind, external_key, generation_id)

    @synchronized
    def release(self, kind, external_key):
        return publication.release(self.conn, kind, external_key)
