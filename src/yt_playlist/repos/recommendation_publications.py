"""Small committed heads, bounded rank pages and directly owned opaque chunks."""
from contextlib import ExitStack
import json
from operator import itemgetter

from yt_playlist.core import recommendation_chunks, recommendation_outputs, recommendation_pools, recording_selection
from yt_playlist.core.audio_vibe_contract import score_version_filter, served_score
from yt_playlist.core.catalog_query import (
    IS_PLAYABLE_SQL, library_tracks_sql, playable_tracks_sql,
)
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.core.recommendation_build_state import release_pool
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core import listening_views
from yt_playlist.library.rating_policy import liked_music_selection
from .base import Repo, lookup_read, snapshot_read, synchronized
from . import recommendation_publication as publication, recommendation_file_writes


class RecommendationPublicationsRepo(Repo):
    def reserve_file(self, build, surface, chunk_key, integrity):
        with publication.staging_write(self) as conn:
            publication._touch(conn, build)
            return recommendation_file_writes.reserve(conn, recommendation_chunks.OWNER, surface, chunk_key, integrity)

    @snapshot_read
    def clicked_pool_header(self, surface):
        from yt_playlist.core import library_pool_state
        with column_group_reads(self._store, 'recommendation_generation_surfaces', 'selection'):
            parameters = library_pool_state.read_parameters(self.conn, surface)
        if parameters is None:
            return None
        generation = self._generation(surface, None)
        return {'generation': generation, 'parameters': parameters}

    @synchronized
    def clear_surface(self, surface):
        with self.conn.atomic():
            self.conn.execute('UPDATE recommendation_generation_surfaces SET selected=0 WHERE selected=1 AND surface=?',
                              (surface,))

    @synchronized
    def remember_clicked_pool_parameters(self, generation, parameters, at):
        from yt_playlist.core import library_pool_state
        with self.conn.atomic():
            library_pool_state.parameters(self.conn, generation, parameters, at)

    def is_private_reader(self):
        return bool(getattr(self._store, '_read_only', False))

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

    @lookup_read
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

    @lookup_read
    def pool_size(self, identity):
        return self.conn.execute('SELECT count(*) FROM recommendation_candidates WHERE pool_id=?', (identity,)).fetchone()[0]

    def append_pool(self, build, identity, candidates, start):
        if len(candidates) > publication.BATCH_ROWS:
            raise ValueError('Recommendation pool append exceeds its batch budget')
        with publication.staging_write(self) as conn:
            publication._touch(conn, build)
            recommendation_pools.append(conn, identity, candidates, start)

    @lookup_read
    def verify_pool(self, identity, prepared):
        # Staging owns an unfinished pool, and sealed membership is immutable.
        # Check that indexed owner without cloning the whole memory database.
        return recommendation_pools.verify(self.conn, identity, prepared)

    def attach_pool(self, build, slot, identity):
        with publication.staging_write(self) as conn:
            publication._touch(conn, build)
            conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=? AND complete=0', (identity,))
            conn.execute('INSERT INTO recommendation_surface_pools '
                '(generation_id,surface,mode_key,purpose,pool_id,revalidated_policy_version) VALUES (?,?,?,?,?,?)',
                (build.generation_id, slot.surface, slot.mode_key, slot.purpose, identity, slot.revalidated_policy))
            release_pool(conn, build.generation_id, identity)

    @lookup_read
    def staged_outputs(self, build, outputs):
        if len(outputs) > publication.OUTPUT_BATCH_ROWS:
            raise ValueError('Recommendation output lookup exceeds its batch budget')
        result = {}
        for output in outputs:
            row = self.conn.execute(f'SELECT content_digest FROM {recommendation_chunks.VIEW} WHERE generation_id=? '
                'AND surface=? AND chunk_key=?', (build.generation_id, output.surface, output.chunk_key)).fetchone()
            if row:
                result[output.surface, output.chunk_key] = row[0]
        return result

    def record_outputs(self, plan, build, files):
        if len(files) > publication.OUTPUT_BATCH_ROWS:
            raise ValueError('Recommendation manifest write exceeds its batch budget')
        with publication.staging_write(self) as conn:
            publication._touch(conn, build)
            for output, file, identity in files:
                recommendation_file_writes.complete(conn, identity, recommendation_chunks.OWNER, output.surface, output.chunk_key, file)
                publication._output(conn, plan, build.generation_id, output, file[0], identity)

    def ready(self, plan, build):
        with publication.staging_write(self) as conn:
            publication._touch(conn, build)
            publication._ready(conn, plan, build.generation_id)

    @synchronized
    def publish(self, build, at, *, retire_surfaces=()):
        with self.conn.atomic():
            result = publication.finish(self.conn, build, at)
            if retire_surfaces:
                self.conn.execute('UPDATE recommendation_generation_surfaces SET selected=0 WHERE selected=1 AND surface IN (' +
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
        with column_group_reads(self._store, 'recommendation_generation_surfaces', 'selection'):
            row = self.conn.execute('SELECT g.* FROM recommendation_generation_surfaces h JOIN recommendation_generations g '
                                    'ON g.id=h.generation_id WHERE h.selected=1 AND h.surface=?', (surface,)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def mix_head_ids(self):
        with column_group_reads(self._store, 'recommendation_generation_surfaces', 'selection'):
            return dict(self.conn.execute("SELECT surface,generation_id FROM recommendation_generation_surfaces "
                                          "WHERE selected=1 AND surface IN ('home','fresh')"))

    @snapshot_read
    def heads(self):
        with column_group_reads(self._store, 'recommendation_generation_surfaces', 'selection'):
            rows = self.conn.execute('SELECT h.surface,g.id,g.output_digest FROM recommendation_generation_surfaces h '
                'JOIN recommendation_generations g ON g.id=h.generation_id '
                'WHERE h.selected=1 ORDER BY h.surface LIMIT 129').fetchall()
        if len(rows) > 128:
            raise WorkBudgetExceeded('Recommendation head inventory exceeds its read budget')
        return [dict(row) for row in rows]

    @lookup_read
    def radio_group_pools_ready(self, surfaces, home_lanes):
        """Check the selected publication's nonempty slots against published leaf versions."""
        row = self.conn.execute('''
            WITH selected AS (
                SELECT h.generation_id FROM json_each(?) AS priority
                JOIN recommendation_generation_surfaces h ON h.selected=1 AND h.surface=priority.value
                ORDER BY priority.key LIMIT 1
            )
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM group_versions v
                JOIN recommendation_surface_pools s ON s.generation_id=selected.generation_id
                    AND s.mode_key=v.external_version
                JOIN recommendation_pools p ON p.id=s.pool_id
                WHERE v.generation_id=h.id AND v.position IS NOT NULL AND v.leaf=1 AND v.legacy=0 AND p.eligible_count>0
                    AND s.surface IN ('radio', 'home')
                    AND CASE s.surface WHEN 'radio' THEN s.purpose='membership'
                        ELSE s.purpose IN (SELECT value FROM json_each(?)) END
            ) THEN 1 WHEN EXISTS (SELECT 1 FROM recommendation_generation_surfaces WHERE selected=1 AND surface='home') THEN 0 END
            FROM selected CROSS JOIN group_generations h WHERE h.is_active=1
        ''', (json.dumps(surfaces), json.dumps(home_lanes))).fetchone()
        # Missing heads can prompt preparation. A shared-only publication can
        # already be serving fallback and just needs its successor to publish.
        return bool(row[0]) if row is not None and row[0] is not None else None

    def _generation(self, surface, generation):
        if generation is None:
            with column_group_reads(self._store, 'recommendation_generation_surfaces', 'selection'):
                row = self.conn.execute('SELECT generation_id FROM recommendation_generation_surfaces '
                                        'WHERE selected=1 AND surface=?', (surface,)).fetchone()
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
        row = self.conn.execute('SELECT p.*,m.revalidated_policy_version AS revalidated_policy FROM recommendation_surface_pools m '
            'JOIN recommendation_pools p ON p.id=m.pool_id '
            'WHERE m.generation_id=? AND m.surface=? '
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
        with ExitStack() as scopes:
            if group_version is not None:
                scopes.enter_context(keyed_reads(self._store, 'group_versions', (group_version,)))
                version = self.conn.execute('SELECT id FROM group_versions WHERE external_version=? AND is_origin=1',
                                            (group_version,)).fetchone()
                scopes.enter_context(keyed_reads(self._store, 'group_members', (version[0],) if version else ()))
            rows = recommendation_pools.page(self.conn, pool['id'], after=after, limit=limit, band=band, genres=genres,
                                             group_version=group_version)
        if rows:
            marks = ','.join('?' for _ in rows)
            keys = dict(self.conn.execute(f'SELECT id,identity_key FROM recordings WHERE id IN ({marks})',
                                          [row['recording_id'] for row in rows]))
            rows = [{**row, 'key': keys[row['recording_id']], 'pool_policy': pool['policy_version']} for row in rows]
        return rows

    @snapshot_read
    def complete_rank_rows(self, slots, *, group_version=None, after=-1, include_metadata=False, fresh_start=None,
                           vibe=None, matching_vibe=False):
        """Read published ranks, optionally with selection facts joined once per recording."""
        slots = tuple(slots)
        if not slots:
            return ([], {}) if include_metadata else []
        selectors = ','.join('(?,?,?,?,?)' for _ in slots)
        params = tuple(value for index, slot in enumerate(slots) for value in (index, *slot))
        rank_params = (after,)
        source_order = ("ROW_NUMBER() OVER (ORDER BY s.slot, m.mode_key<>'all', m.mode_key) AS source_order, "
                        if include_metadata else '')
        candidate_join = 'CROSS JOIN' if include_metadata else 'JOIN'
        with ExitStack() as scopes:
            if include_metadata and not self.conn.in_transaction:
                # Metadata and its multi-artist relation must see the same commit.
                self.conn.execute('BEGIN')
                scopes.callback(self.conn.rollback)
            members = ''
            if group_version is not None:
                scopes.enter_context(keyed_reads(self._store, 'group_versions', (group_version,)))
                if getattr(self._store, '_snapshot_reads', None) is not None:
                    version = self.conn.execute('SELECT id FROM group_versions WHERE external_version=? AND is_origin=1',
                                                (group_version,)).fetchone()
                    scopes.enter_context(keyed_reads(self._store, 'group_members', (version[0],) if version else ()))
                members = '''AND c.recording_id IN (
                    SELECT gm.recording_id FROM group_versions v
                    JOIN group_members gm ON gm.group_version_id=v.id
                    WHERE v.external_version=? AND v.is_origin=1 AND v.complete=1
                )'''
                rank_params += (group_version,)
            matches = ''
            ranked_source = f'pools p {candidate_join} resolved_recommendation_candidates c ON c.pool_id=p.pool_id'
            if matching_vibe:
                matches = f''', vibe_scores AS MATERIALIZED (
                    SELECT fit.recording_id, {served_score('fit')} AS effective_fit
                    FROM served_vibe_fits fit
                    JOIN recordings r ON r.id=fit.recording_id AND r.musical_profile_version IS NOT NULL
                    WHERE fit.vibe=? AND {score_version_filter('fit.')}
                    GROUP BY fit.recording_id
                    HAVING effective_fit > 0.5 AND effective_fit <= 1
                )'''
                ranked_source = '''vibe_scores v CROSS JOIN pools p
                    CROSS JOIN recommendation_candidates c ON c.pool_id=p.pool_id AND c.recording_id=v.recording_id'''
                params += vibe
            params += rank_params
            pools = f'''
                WITH requested(slot,surface,generation_id,mode_key,purpose) AS (
                    VALUES {selectors}
                ), pools AS MATERIALIZED (
                    SELECT {source_order}s.slot, m.generation_id, m.mode_key, m.purpose, m.pool_id,
                           COALESCE(m.revalidated_policy_version, p.policy_version) AS pool_policy
                    FROM requested s
                    LEFT JOIN recommendation_generation_surfaces h
                      ON h.selected=1 AND h.surface=s.surface AND s.generation_id IS NULL
                    JOIN recommendation_surface_pools m ON m.generation_id=COALESCE(s.generation_id, h.generation_id)
                     AND m.surface=s.surface AND m.purpose=s.purpose AND (s.mode_key IS NULL OR m.mode_key=s.mode_key)
                    JOIN recommendation_generations g ON g.id=m.generation_id AND g.status='published'
                    JOIN recommendation_pools p ON p.id=m.pool_id
                ){matches}, ranked AS NOT MATERIALIZED (
                    SELECT p.*, c.recording_id, c.rank, c.score
                    FROM {ranked_source}
                    WHERE c.rank>? {members}
                )'''
            ranks = '''
                SELECT p.slot,p.generation_id,p.mode_key,p.purpose,p.pool_id,p.pool_policy,
                       p.recording_id,r.identity_key AS key,p.rank,p.score
                FROM ranked p JOIN recordings r ON r.id=p.recording_id'''
            order = "ORDER BY slot, mode_key<>'all', mode_key, rank"
            if include_metadata:
                return self._ranks_with_metadata(pools, params, slots, fresh_start, vibe, matching_vibe)
            rows = self.conn.execute(f'{pools} {ranks} {order}', params).fetchall()
            return [dict(row) for row in rows]

    @snapshot_read
    def output_reference(self, surface, chunk_key, *, generation=None):
        generation = self._generation(surface, generation)
        if generation is None:
            return None
        with recommendation_chunks.generation_reads(self.conn, (generation,)):
            row = self.conn.execute(f'SELECT filename,content_digest,byte_size FROM {recommendation_chunks.VIEW} '
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
        with recommendation_chunks.generation_reads(self.conn, (generation,)):
            chunks = self.conn.execute('WITH wanted(rank) AS (VALUES ' + values + ') '
                'SELECT DISTINCT o.chunk_key,o.filename,o.content_digest,o.byte_size FROM wanted w '
                f'JOIN {recommendation_chunks.VIEW} o ON o.id=(SELECT id FROM {recommendation_chunks.VIEW} '
                'WHERE generation_id=? AND surface=? AND pool_id=? AND first_rank<=w.rank '
                'ORDER BY first_rank DESC LIMIT 1) WHERE o.last_rank>=w.rank',
                (*wanted, generation, surface, pool['id'])).fetchall()
        if sum(row['byte_size'] for row in chunks) > 4 * recommendation_outputs.MAX_BYTES:
            raise WorkBudgetExceeded('Prepared display lookahead exceeds its read budget')
        return [dict(row) for row in chunks]

    @snapshot_read
    def mix_display_references(self, selections):
        """Read the explanation file owners for the selected tracks in one statement."""
        requested = [(surface, generation, pool, rank)
                     for (surface, generation, pool), ranks in selections.items()
                     for rank in ranks]
        if not requested:
            return []
        values = ','.join('(?,?,?,?)' for _ in requested)
        with recommendation_chunks.generation_reads(self.conn, {entry[1] for entry in requested}):
            rows = self.conn.execute(f'''
                WITH wanted(surface,generation,pool,rank) AS (VALUES {values})
                SELECT DISTINCT o.surface,o.generation_id,o.pool_id,o.chunk_key,
                                o.filename,o.content_digest,o.byte_size
                FROM wanted w
                JOIN {recommendation_chunks.VIEW} o ON o.id=(
                    SELECT id FROM {recommendation_chunks.VIEW}
                    WHERE generation_id=w.generation AND surface=w.surface AND pool_id=w.pool
                      AND first_rank<=w.rank
                    ORDER BY first_rank DESC LIMIT 1
                ) WHERE o.last_rank>=w.rank
            ''', tuple(value for entry in requested for value in entry))
        references = []
        sizes = {}
        for row in rows:
            source = row['surface'], row['generation_id'], row['pool_id']
            sizes[source] = sizes.get(source, 0) + row['byte_size']
            if sizes[source] > 4 * recommendation_outputs.MAX_BYTES:
                raise WorkBudgetExceeded('Prepared display lookahead exceeds its read budget')
            references.append(dict(row))
        return references


    @lookup_read
    def framing(self, surface, *, generation=None):
        generation = self._generation(surface, generation)
        if generation is None:
            return None
        row = self.conn.execute(f'SELECT id AS generation_id,{publication.FRAMING_COLUMNS} FROM recommendation_generations '
                                'WHERE id=? AND presence IS NOT NULL',
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
    def home_genre_candidates(self):
        """Read one artist and current genre per playable recording in the published Home pools."""
        library = playable_tracks_sql(library_tracks_sql(
            'SELECT r.id,r.identity_key FROM members m JOIN recordings r ON r.id=m.recording_id'
        ))
        return [dict(row) for row in self.conn.execute(f'''
            WITH members AS MATERIALIZED (
                SELECT DISTINCT recording_id
                FROM recommendation_candidates
                WHERE pool_id IN (
                    SELECT m.pool_id
                    FROM recommendation_generation_surfaces h
                    JOIN recommendation_surface_pools m
                      ON m.generation_id=h.generation_id AND m.surface=h.surface
                    WHERE h.selected=1 AND h.surface='home'
                )
            ), library AS ({library})
            SELECT track.artist, (
                SELECT genre FROM catalog_tracks
                WHERE recording_id=members.recording_id AND genre<>''
                ORDER BY id DESC LIMIT 1
            ) AS genre
            FROM members
            JOIN catalog_tracks track ON track.id=COALESCE(
                (SELECT l.id FROM library l WHERE l.recording_id=members.recording_id),
                (SELECT d.id FROM discovered_tracks d
                 WHERE d.recording_id=members.recording_id AND {IS_PLAYABLE_SQL})
            )
        ''')]

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
            'SELECT DISTINCT intensity_band,genre_key FROM resolved_recommendation_candidates '
            'WHERE pool_id=? AND intensity_band IS NOT NULL' + predicate + ' ORDER BY intensity_band,genre_key LIMIT ?',
            (pool['id'], *(after or ()), *((band,) if band is not None else ()), limit))]

    @snapshot_read
    def intensity_bands(self, surface, mode_key, purpose, *, generation=None):
        pool = self.pool(surface, mode_key, purpose, generation=generation)
        if pool is None:
            return []
        return [row[0] for row in self.conn.execute('SELECT DISTINCT intensity_band '
            'FROM resolved_recommendation_candidates WHERE pool_id=? AND intensity_band IS NOT NULL '
            'ORDER BY intensity_band LIMIT 51', (pool['id'],))]

    @snapshot_read
    def intensity_headings(self, surface, *, generation=None):
        """Read the immutable band/genre headings for all published Radio styles."""
        generation = self._generation(surface, generation)
        if generation is None:
            return {}
        rows = self.conn.execute(
            'SELECT DISTINCT m.mode_key,b.intensity_band,b.genre_key FROM recommendation_surface_pools m '
            'CROSS JOIN resolved_recommendation_candidates b WHERE m.generation_id=? AND m.surface=? '
            "AND m.purpose='membership' AND b.pool_id=m.pool_id AND b.intensity_band IS NOT NULL "
            'ORDER BY m.mode_key,b.intensity_band,b.genre_key',
            (generation, surface))
        headings = {}
        for mode, band, genre in rows:
            headings.setdefault(mode, {}).setdefault(band, []).append(genre)
        return headings

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
        whole-history eligibility calculation. Materialize the requested subject
        inputs first so the correlated eligibility probes can index their keys.
        Both sides of those probes refer to the same recording, so unrelated
        subjects cannot affect the result.
        """
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return {}
        names = {name: 'radio_' + name for name in
                 ('play_event_subjects', 'listening_observation_subjects',
                  'eligible_listening_evidence', 'listening_day_counts')}
        names.update(sources='play_event_sources', calendar='play_event_calendar')
        values = ','.join('(?)' for _ in keys)
        inputs = [f'requested(value) AS MATERIALIZED (VALUES {values})']
        inputs.extend(f'{names[name]} AS MATERIALIZED (SELECT * FROM {name} '
                      'WHERE identity_key IN (SELECT value FROM requested))'
                      for name in ('play_event_subjects', 'listening_observation_subjects'))
        inputs.extend(f'{names[name]} AS ({query})'
                      for name, query in listening_views.definitions(lambda name: names.get(name, name))
                      if name in ('eligible_listening_evidence', 'listening_day_counts'))
        return dict(self.conn.execute('WITH ' + ','.join(inputs) +
            ' SELECT identity_key,plays FROM ' + names['listening_day_counts'], keys))

    def _ranks_with_metadata(self, pools, params, slots, fresh_start, vibe, matching_vibe):
        # Each library lane keeps its own rank. Fresh uses the first source
        # that ranks a recording, in the same order the Fresh picker consumes.
        lane_count = len(slots) if fresh_start is None else fresh_start + 1
        lane = 'slot' if fresh_start is None else f'MIN(slot, {fresh_start})'
        rank_sources = ', '.join(f'MIN(source_order) FILTER (WHERE {lane}={index}) AS source_{index}'
                                 for index in range(lane_count))
        metadata_columns = ('recording_id', 'key', 'artist', 'album', 'genre',
                            'duration', 'year', '_radio_fresh', 'primary_credit')
        if vibe:
            metadata_columns += ('vibe_fit', 'orig_title', 'artist_browse_id')
        rank_columns = []
        rank_joins = []
        rank_offsets = []
        fixed_ranks = []
        for index in range(lane_count):
            pool, rank = f'p{index}', f'c{index}'
            offset = len(metadata_columns) + len(rank_columns)
            _, generation, mode, _ = slots[index]
            # Pinned slots already supply their source. The combined Fresh lane
            # may choose a different slot or mode for each recording.
            if index != fresh_start and type(generation) is int and type(mode) is str:
                source = (index, generation, mode)
                for field in ('pool_id', 'pool_policy', 'rank', 'score'):
                    alias = f'{field}_{index}'
                    fixed_ranks.append(f'MAX({field}) FILTER (WHERE slot={index}) AS {alias}')
                    rank_columns.append(f'q.{alias}')
            else:
                source = None
                rank_columns.extend((f'{pool}.pool_id', f'{pool}.pool_policy', f'{rank}.rank', f'{rank}.score',
                                     f'{pool}.slot', f'{pool}.generation_id', f'{pool}.mode_key'))
                rank_joins.append(f'LEFT JOIN pools {pool} ON {pool}.source_order=q.source_{index}')
                rank_joins.append(f'LEFT JOIN recommendation_candidates {rank} '
                                  f'ON {rank}.pool_id={pool}.pool_id AND {rank}.recording_id=q.recording_id')
            rank_offsets.append((offset, source))
        rank_sources = ', '.join((rank_sources, *fixed_ranks))
        ranked_recordings = f'''{pools},
            recording_ranks AS MATERIALIZED (
                SELECT recording_id, {rank_sources}
                FROM ranked GROUP BY recording_id
            )'''
        source = 'r'
        source_joins = ''
        playable = 'AND r.has_playable_source'
        if not recording_selection.ready(self.conn):
            ranked_recordings += f''', preferred_sources AS MATERIALIZED (
                SELECT q.recording_id,{recording_selection.PREFERRED_SOURCE_ID} AS id
                FROM recording_ranks q JOIN track_sources s ON s.recording_id=q.recording_id
                WHERE {recording_selection.PLAYABLE_SOURCE}
                GROUP BY q.recording_id
            )'''
            source = 's'
            source_joins = ('JOIN preferred_sources chosen ON chosen.recording_id=r.id '
                            'JOIN track_sources s ON s.id=chosen.id')
            playable = ''
        lane_columns = ', '.join(rank_columns)
        lane_joins = ' '.join(rank_joins)
        vibe_column = vibe_join = ''
        edit_columns = ''
        if vibe:
            edit_source = source
            if source == 'r':
                # Use the same playable upload as mix_playback_details. Keep
                # full edit titles on the upload, including version qualifiers.
                edit_source = 'edit'
                source_joins += f'''LEFT JOIN track_sources edit ON edit.id=(
                    SELECT s.id FROM track_sources s WHERE s.recording_id=r.id
                    AND {recording_selection.PLAYABLE_SOURCE}
                    ORDER BY {recording_selection.SOURCE_PREFERENCE} LIMIT 1)'''
            edit_columns = f'{edit_source}.effective_orig_title,{edit_source}.effective_artist_browse_id,'
        if matching_vibe:
            vibe_column = 'v.effective_fit,'
            vibe_join = 'JOIN vibe_scores v ON v.recording_id=q.recording_id'
        elif vibe:
            ranked_recordings += f''', vibe_scores AS MATERIALIZED (
                SELECT q.recording_id, {served_score('fit')} AS score
                FROM served_vibe_fits fit JOIN recording_ranks q ON q.recording_id=fit.recording_id
                WHERE fit.vibe=? AND {score_version_filter('fit.')}
                GROUP BY q.recording_id
            )'''
            params += vibe
            vibe_column = 'v.score,'
            vibe_join = ('LEFT JOIN vibe_scores v ON v.recording_id=r.id '
                         'AND r.musical_profile_version IS NOT NULL')
        query = f'''{ranked_recordings}
            SELECT r.id,r.identity_key,r.effective_artist,{source}.effective_album,
                   COALESCE(g.name,r.effective_unresolved_genre),{source}.effective_duration_s,
                   {source}.effective_mb_year,r.is_discovery AND NOT r.has_eligible_listen,
                   r.primary_credit_artist_id,{vibe_column}{edit_columns}{lane_columns}
            FROM recording_ranks q CROSS JOIN recordings r ON r.id=q.recording_id
            {source_joins}
            LEFT JOIN genre_catalog gd ON gd.key=r.effective_genre_key
            LEFT JOIN genre_catalog g ON g.key=COALESCE(gd.redirect_key,r.effective_genre_key)
            {vibe_join}
            {lane_joins}
            WHERE r.redirect_recording_id IS NULL {playable}
              AND (r.has_saved_source OR r.has_eligible_listen OR r.in_collection_album OR r.is_discovery)
        '''
        rows = self.conn.execute(query, params)
        rows.row_factory = None
        ranks_by_slot = [[] for _ in slots]
        facts = {}
        for row in rows.fetchall():
            recording_id = row[0]
            facts[recording_id] = dict(zip(metadata_columns, row))
            for offset, source in rank_offsets:
                if (pool_id := row[offset]) is not None:
                    slot, generation, mode = source if source is not None else row[offset + 4:offset + 7]
                    ranks_by_slot[slot].append((generation, mode, pool_id, row[offset + 1],
                                                row[offset + 2], row[offset + 3], recording_id))
        if facts:
            credits = self.conn.execute('SELECT recording_id,artist_id FROM recording_additional_artists')
            credits.row_factory = None
            for recording_id, artist_id in credits.fetchall():
                if (track := facts.get(recording_id)) is not None:
                    track.setdefault('additional_credits', []).append(artist_id)
        rank_key = itemgetter(4)
        for slot, ranks in zip(slots, ranks_by_slot, strict=True):
            if slot[2] is None:
                ranks.sort(key=lambda row: (row[1] != 'all', row[1], row[4]))
            else:
                ranks.sort(key=rank_key)
        return ranks_by_slot, facts

    @snapshot_read
    def candidate_details(self, videos):
        videos = tuple(dict.fromkeys(videos))
        if not videos:
            return {}
        placeholders = ','.join('?' for _ in videos)
        rows = self._candidate_details('t.duration_s AS duration,t.orig_title,t.artist_browse_id',
                                      f'WHERE t.video_id IN ({placeholders})', videos)
        return {row['video_id']: dict(row) for row in rows}

    @lookup_read
    def mix_playback_details(self, recordings):
        recordings = tuple(dict.fromkeys(recordings))
        if not recordings:
            return {}
        placeholders = ','.join('(?)' for _ in recordings)
        sources = f'''WITH requested(recording_id) AS (VALUES {placeholders}), chosen AS (
            SELECT q.recording_id,{recording_selection.PREFERRED_SOURCE_ID} AS id
            FROM requested q JOIN recordings r ON r.id=q.recording_id
            CROSS JOIN track_sources s ON s.recording_id=COALESCE(r.redirect_recording_id,r.id)
            WHERE {recording_selection.PLAYABLE_SOURCE}
            GROUP BY q.recording_id
        )'''
        rows = self._candidate_details(
            'chosen.recording_id,t.title,t.artist,t.album,t.thumbnail,t.duration_s AS duration,'
            't.orig_title,t.artist_browse_id',
            'JOIN chosen ON chosen.id=t.id', (), cte=sources, cte_params=recordings)
        return {row['recording_id']: dict(row) for row in rows}

    def _candidate_details(self, projection, source_filter, params, *, cte='', cte_params=()):
        with liked_music_selection(self._store) as (liked, likes):
            return self.conn.execute(
                f'{cte} SELECT t.video_id,{projection},t.album_browse_id,t.bpm,t.danceability,'
                f't.available,{liked} AS liked FROM catalog_tracks t '
                f'{source_filter}', (*cte_params, *likes, *params)).fetchall()

    @snapshot_read
    def catalog_read_version(self):
        return self.conn.execute('PRAGMA data_version').fetchone()[0]

    @snapshot_read
    def vibe_fresh_candidates(self, surface, generation, *, include, avoid, limit):
        """Read the selected Fresh reserves and their current Discovery entries in one query."""
        params = [generation, surface]
        modes = "AND m.mode_key=''"
        rank = 'AND c.rank<?'
        if include or avoid:
            families = []
            for values, predicate in ((include, 'IN'), (avoid, 'NOT IN')):
                if values:
                    families.append(f'm.mode_key {predicate} ({",".join("?" for _ in values)})')
                    params.extend(sorted(values))
            modes = "AND (m.mode_key='' OR (" + ' AND '.join(families) + '))'
            rank = ''
        else:
            params.append(limit)
        with liked_music_selection(self._store) as (liked, likes):
            rows = self.conn.execute(f'''
                SELECT c.recording_id,c.rank,c.score,m.pool_id,r.identity_key AS key,
                       COALESCE(m.revalidated_policy_version,p.policy_version) AS pool_policy,
                       t.title,t.artist,t.album,t.video_id,t.thumbnail,t.genre,t.mb_year AS year,
                       t.duration_s AS duration,t.orig_title,t.artist_browse_id,
                       t.album_browse_id,t.bpm,t.danceability,t.available,
                       {liked} AS liked
                FROM recommendation_surface_pools m JOIN recommendation_pools p ON p.id=m.pool_id
                CROSS JOIN recommendation_candidates c ON c.pool_id=m.pool_id
                CROSS JOIN recordings r ON r.id=c.recording_id
                JOIN discovered_tracks t ON t.recording_id=r.id
                WHERE m.generation_id=? AND m.surface=? AND m.purpose='fresh' {modes} {rank}
                    AND r.redirect_recording_id IS NULL AND t.video_id IS NOT NULL AND t.available IS NOT 0
                    AND NOT (r.has_saved_source OR r.has_eligible_listen OR r.in_collection_album)
                ORDER BY m.mode_key<>'',m.mode_key,c.rank
            ''', (*likes, *params)).fetchall()
        return [dict(row) for row in rows]

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
