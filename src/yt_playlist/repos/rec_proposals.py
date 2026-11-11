"""Last-good prepared recommendations and pre-encoded snapshot publication.

The snapshot publisher owns the transaction for publish_prepared_* methods. Policy
rows belong to rec_ledger and must participate in that same outer transaction.
"""
from yt_playlist.core import choice_learning_state, group_preview_work, home_outputs, home_revisit_progress, json_cache
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.repos import proposal_cache, proposal_storage


class RecProposalsRepo(Repo):
    @snapshot_read
    def preview_work(self):
        return group_preview_work.read(self.conn)

    def save_preview_page(self, snapshot, header, expected_token, page, dependencies):
        from yt_playlist.core.db_checkpoint import deferred_autocheckpoint
        from yt_playlist.core.db_writes import atomic_write

        # A completed page's short durable receipt drains even when shutdown
        # has just been requested. The next page observes cancellation normally.
        from contextvars import Context

        def save():
            with atomic_write(self._store), deferred_autocheckpoint(self.conn, self._store._db_path is not None):
                if not snapshot.is_current(allow_stale_tables=group_preview_work.TABLES):
                    return False
                return group_preview_work.save(self.conn, header, expected_token, page, dependencies)

        return Context().run(save)

    @lookup_read
    def revisit_remaining(self):
        return home_revisit_progress.read(self.conn)

    @synchronized
    def save_revisit_remaining(self, remaining):
        """Join the guarded summary publication when one is active."""
        with self.conn.atomic(join=True):
            home_revisit_progress.save(self.conn, remaining)

    @snapshot_read
    def cache_rows(self):
        return proposal_cache.logical_rows(self.conn, store=self._store)

    def stage_pruned_cache(self, prepared):
        return [(proposal_cache.stage(self, surface, encoded) if surface in proposal_cache.SURFACES else encoded,
                 surface) for encoded, surface in prepared]

    @synchronized
    def publish_pruned_cache(self, prepared):
        from yt_playlist.repos.discovery_cache import publish
        with self.conn.atomic():
            publish(self.conn, prepared)

    @synchronized
    def discard_learned_outputs(self):
        from yt_playlist.core import recommendation_setting_outputs
        from yt_playlist.core.recommendation_invalidation import clear_display, clear_findings
        with self.conn.atomic():
            self.conn.execute('DELETE FROM rec_proposals')
            clear_display(self.conn)
            clear_findings(self.conn)
            if recommendation_setting_outputs.available(self.conn):
                recommendation_setting_outputs.clear(self.conn)

    @snapshot_read
    def group_pool_source(self):
        from yt_playlist.core.db_revisions import keyed_reads

        encoded = proposal_cache.read(self.conn, 'mode_bundles', store=self._store)
        if encoded is not None:
            return encoded, None
        with keyed_reads(self._store, 'rec_proposals', ('mode_bundles',)):
            row = self.conn.execute("SELECT CASE WHEN json_extract(payload,?) IN (?,?) "
                "THEN json_extract(payload,'$.groups') ELSE "
                "json_object('all',COALESCE(json_extract(payload,'$.all'),json('{}')),"
                "'_meta',COALESCE(json_extract(payload,'$._meta'),json('{}'))) END,"
                "json_extract(payload,?),json_extract(payload,'$.profiles') FROM rec_proposals "
                "WHERE surface='mode_bundles' AND json_valid(payload)",
                ('$.' + json_cache.MARKER, json_cache.FORMAT, json_cache.LEGACY_FORMAT,
                 '$.' + json_cache.MARKER)).fetchone()
            return None, row

    @lookup_read
    def group_checkpoint(self):
        """Small serving identity without decoding the prepared candidate lists."""
        from yt_playlist.core.db_revisions import keyed_reads
        from yt_playlist.core.preparation import home_publication

        with keyed_reads(self._store, 'rec_proposals', ('adaptive_group_preview',)):
            handled, header = home_publication.header(self.conn, 'adaptive_group_preview')
            if handled:
                return dict(zip(('schema', 'generation', 'mix_regions'),
                    (header['schema_version'], header['edition_key'], header['mix_regions']))) if header is not None else None
        if self._store._home_display_files:
            return home_outputs.checkpoint(self.conn)
        with keyed_reads(self._store, 'rec_proposals', ('adaptive_group_preview',)):
            row = self.conn.execute("SELECT json_extract(payload,'$.schema'),"
                "json_extract(payload,'$.generation'),"
                "COALESCE(json_array_length(payload,'$.menus.wheelhouse'),0)+"
                "COALESCE(json_array_length(payload,'$.menus.explore'),0)+"
                "COALESCE(json_array_length(payload,'$.menus.comfort'),0)+"
                "COALESCE(json_array_length(payload,'$.menus.temporal'),0) FROM rec_proposals "
                "WHERE surface='adaptive_group_preview' AND json_valid(payload)").fetchone()
        return {'schema': row[0], 'generation': row[1], 'mix_regions': row[2]} if row else None

    def stage_prepared_bundles(self, bundles_json, fresh_json):
        """Commit bounded hidden chunks before entering the guarded publication."""
        return (proposal_cache.stage(self, 'mode_bundles', bundles_json),
                proposal_cache.stage(self, 'fresh_pool', fresh_json))

    @synchronized
    def publish_prepared_bundles(self, bundles_json, fresh_json, now) -> None:
        """Switch staged output; legacy encoded callers retain their transaction contract."""
        if any(isinstance(value, proposal_cache.PreparedCache) for value in (bundles_json, fresh_json)):
            if (not isinstance(bundles_json, proposal_cache.PreparedCache)
                    or bundles_json.surface != 'mode_bundles'
                    or not isinstance(fresh_json, proposal_cache.PreparedCache)
                    or fresh_json.surface != 'fresh_pool'):
                raise ValueError('Prepared recommendation surfaces do not match')
            proposal_cache.finish(self.conn, bundles_json, now)
            proposal_cache.finish(self.conn, fresh_json, now)
            return
        self.conn.executemany(
            "INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
            [("mode_bundles", bundles_json, now), ("fresh_pool", fresh_json, now)])

    @synchronized
    def publish_prepared_home(self, payload, now, *, surface="home_prepared", input_revision=None) -> None:
        """Publish pre-encoded Home output; the snapshot publisher owns the transaction."""
        proposal_storage.publish(self._store, surface, payload, now, input_revision=input_revision)

    @synchronized
    def put_encoded(self, surface, encoded, now):
        with self.conn:
            proposal_storage.publish(self._store, surface, encoded, now, standalone=True)

    @snapshot_read
    def choice_refresh_inputs(self):
        """Read current choice identities and decay evidence through their installed owner."""
        surfaces = ('choice_interests', 'choice_bandit')
        if self._store._choice_learning:
            return tuple(choice_learning_state.refresh_input(self.conn, surface, store=self._store) or {}
                         for surface in surfaces)
        return None

    @snapshot_read
    def has_proposals(self, surface):
        """Check dependency presence without reading or decoding a large display pool."""
        return proposal_storage.present(self._store, surface)

    @snapshot_read
    def document(self, surface):
        return proposal_storage.document(self._store, surface)

