"""Small committed heads, bounded rank pages and directly owned opaque chunks."""
from yt_playlist.core import recommendation_outputs, recommendation_pools, recommendation_revalidations
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core import listening_subject_reads
from yt_playlist.core.preparation import listening_views
from yt_playlist.library.rating_policy import LIKED_EXISTS
from .base import Repo, lookup_read, snapshot_read, synchronized
from . import recommendation_publication as publication, proposal_cache


class RecommendationPublicationsRepo(Repo):
    @synchronized
    def remember_clicked_pool_parameters(self, generation, parameters, at):
        from yt_playlist.core import library_pool_state
        with self.conn.atomic():
            library_pool_state.parameters(self.conn, generation, parameters, at)

    def is_private_reader(self):
        return bool(getattr(self._store, '_read_only', False))

    @snapshot_read
    def supports_pool_revalidation(self):
        from .bundle_storage import BundleInputs
        return BundleInputs(self.conn).supports_pool_revalidation()

    @snapshot_read
    def recording_ids(self, keys):
        from .bundle_storage import BundleInputs
        return BundleInputs(self.conn).recording_ids(keys)

    @snapshot_read
    def policy_families(self, versions):
        from .bundle_storage import BundleInputs
        return BundleInputs(self.conn).policy_families(versions)

    @snapshot_read
    def recent_pools(self, *, generations, limit):
        from .bundle_storage import BundleInputs
        return BundleInputs(self.conn).recent_pools(generations=generations, limit=limit)

    @snapshot_read
    def pool_matches(self, identity, prepared):
        from .bundle_storage import BundleInputs
        return BundleInputs(self.conn).pool_matches(identity, prepared)

    def claim(self, plan, digest, *, owner=None):
        return publication.claim(self, plan, digest, owner=owner)

    @snapshot_read
    def staged_slots(self, build, slots):
        if len(slots) > publication.SLOT_BATCH_ROWS:
            raise ValueError('Recommendation slot lookup exceeds its batch budget')
        result = {}
        for slot in slots:
            row = self.conn.execute('SELECT p.content_digest FROM recommendation_surface_pools m '
                'JOIN recommendation_pools p ON p.id=m.pool_id WHERE m.generation_id=? AND m.surface=? '
                'AND m.mode_key=? AND m.purpose=?',
                (build.generation_id, slot.surface, slot.mode_key, slot.purpose)).fetchone()
            if row:
                result[slot.surface, slot.mode_key, slot.purpose] = row[0]
        return result

    def begin_pool(self, build, prepared):
        return publication.begin_pool(self, build, prepared)

    @snapshot_read
    def pool_size(self, identity):
        return self.conn.execute('SELECT (SELECT count(*) FROM recommendation_candidates WHERE pool_id=?)'
            '+(SELECT count(*) FROM unresolved_recommendation_candidates WHERE pool_id=?)',
            (identity, identity)).fetchone()[0]

    def append_pool(self, build, identity, candidates, start):
        if len(candidates) > publication.BATCH_ROWS:
            raise ValueError('Recommendation pool append exceeds its batch budget')
        with proposal_cache._write(self) as conn:
            publication._touch(conn, build)
            recommendation_pools.append(conn, identity, candidates, start)

    @snapshot_read
    def verify_pool(self, identity, prepared):
        return recommendation_pools.verify(self.conn, identity, prepared)

    def attach_pool(self, build, slot, identity):
        with proposal_cache._write(self) as conn:
            publication._touch(conn, build)
            conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=? AND complete=0', (identity,))
            if slot.revalidated:
                conn.execute('INSERT INTO recommendation_pool_revalidations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                    (build.generation_id, slot.surface, slot.mode_key, slot.purpose, identity, slot.revalidated_policy))
            conn.execute('INSERT INTO recommendation_surface_pools VALUES (?,?,?,?,?)',
                (build.generation_id, slot.surface, slot.mode_key, slot.purpose, identity))
            conn.execute('DELETE FROM recommendation_build_pools WHERE generation_id=? AND pool_id=?',
                (build.generation_id, identity))

    @snapshot_read
    def staged_outputs(self, build, outputs):
        if len(outputs) > publication.OUTPUT_BATCH_ROWS:
            raise ValueError('Recommendation output lookup exceeds its batch budget')
        result = {}
        for output in outputs:
            row = self.conn.execute('SELECT content_digest FROM prepared_outputs WHERE generation_id=? '
                'AND surface=? AND chunk_key=?', (build.generation_id, output.surface, output.chunk_key)).fetchone()
            if row:
                result[output.surface, output.chunk_key] = row[0]
        return result

    def record_outputs(self, plan, build, files):
        if len(files) > publication.OUTPUT_BATCH_ROWS:
            raise ValueError('Recommendation manifest write exceeds its batch budget')
        with proposal_cache._write(self) as conn:
            publication._touch(conn, build)
            for output, file in files:
                publication._output(conn, plan, build.generation_id, output, file)

    def ready(self, plan, build):
        with proposal_cache._write(self) as conn:
            publication._touch(conn, build)
            publication._ready(conn, plan, build.generation_id)

    @synchronized
    def publish(self, build, at, *, retire_surfaces=()):
        with self.conn.atomic():
            result = publication.finish(self.conn, build, at)
            if retire_surfaces:
                self.conn.execute('DELETE FROM recommendation_heads WHERE surface IN (' +
                                  ','.join('?' for _ in retire_surfaces) + ')', tuple(retire_surfaces))
            return result

    def cleanup(self):
        return publication.cleanup(self)

    def cleanup_generations(self, **options):
        return publication.cleanup_generations(self, **options)

    def cleanup_pools(self, **options):
        return publication.cleanup_pools(self, **options)

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
    def output_reference(self, surface, chunk_key, *, generation=None):
        generation = self._generation(surface, generation)
        if generation is None:
            return None
        row = self.conn.execute('SELECT filename,content_digest,byte_size FROM prepared_outputs '
            'WHERE generation_id=? AND surface=? AND chunk_key=?', (generation, surface, chunk_key)).fetchone()
        if row is None:
            return None
        return dict(row)

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
    def recording_metadata_rows(self, surface, *, generation=None, after=0, limit=128, mode_key=None):
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
            return None, []
        mode_clause = ' AND m.mode_key=?' if mode_key is not None else ''
        rows = self.conn.execute('SELECT r.id,r.identity_key FROM recordings r WHERE r.id>? AND EXISTS('
            'SELECT 1 FROM recommendation_candidates c JOIN recommendation_surface_pools m ON m.pool_id=c.pool_id '
            'WHERE c.recording_id=r.id AND m.generation_id=? AND m.surface=?' + mode_clause + ') '
            'ORDER BY r.id LIMIT ?', (after, generation, surface,
                                     *((mode_key,) if mode_key is not None else ()), limit)).fetchall()
        return generation, [dict(row) for row in rows]

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
    def candidate_rows(self, surface, selections, *, generation=None, limit=128,
                       band=None, genres=None, group_version=None):
        """Capture bounded pool owners and rank rows without catalog composition."""
        if len(selections) > 16:
            raise ValueError('Candidate batch exceeds 16 pages')
        generation = self._generation(surface, generation)
        pages = []
        for mode_key, purpose, after in selections:
            pool = self.pool(surface, mode_key, purpose, generation=generation) if generation is not None else None
            rows = (self.page(surface, mode_key, purpose, generation=generation, after=after,
                             limit=limit, band=band, genres=genres, group_version=group_version)
                    if pool else [])
            pages.append((mode_key, purpose, pool, rows))
        return generation, pages

    @snapshot_read
    def candidate_play_counts(self, keys):
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

    @snapshot_read
    def candidate_details(self, videos):
        details = {}
        for offset in range(0, len(videos), 500):
            batch = videos[offset:offset + 500]
            marks = ','.join('?' for _ in batch)
            details.update({row['video_id']: dict(row) for row in self.conn.execute(
                'SELECT t.video_id,t.album_browse_id,t.duration_s AS duration,t.bpm,t.energy,t.danceability,'
                f't.available,{LIKED_EXISTS} AS liked FROM catalog_tracks t WHERE t.video_id IN ({marks})', batch)})
        return details

    @snapshot_read
    def selected_rows(self, surface, selections, *, generation=None):
        """Capture exact bounded selections from committed rank pools."""
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
            pages.append((mode, purpose, pool, rows))
        return generation, pages

    @synchronized
    def retain(self, kind, external_key, generation_id):
        return publication.retain(self.conn, kind, external_key, generation_id)

    @synchronized
    def release(self, kind, external_key):
        return publication.release(self.conn, kind, external_key)
