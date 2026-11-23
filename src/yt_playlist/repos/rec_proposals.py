"""Current proposal state and the inputs required to refresh it."""
import json

from yt_playlist.core import (choice_learning_state, home_outputs, home_revisit_progress,
    library_taste_summary, recommendation_requests, worker_settings)
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.repos import proposal_storage


class RecProposalsRepo(Repo):
    @snapshot_read
    def library_taste_summary(self):
        return library_taste_summary.read(self.conn)

    @synchronized
    def publish_library_taste_summary(self, summary, now):
        with self.conn.atomic():
            library_taste_summary.publish(self.conn, summary, now)

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
        return [dict(row) for row in self.conn.execute('SELECT surface,payload FROM rec_proposals')]

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
            library_taste_summary.clear(self.conn)
            clear_display(self.conn)
            clear_findings(self.conn)
            recommendation_setting_outputs.clear(self.conn)

    @lookup_read
    def group_checkpoint(self):
        """Small serving identity without decoding the prepared candidate lists."""
        return home_outputs.checkpoint(self.conn)

    @lookup_read
    def preview_recommendations(self, generation, schema):
        return home_outputs.preview_recommendations(self.conn, generation, schema)

    @lookup_read
    def vibe_request(self):
        raw = worker_settings.read(self.conn, recommendation_requests.HOME_VIBE)
        return json.loads(raw) if raw is not None else None

    @synchronized
    def save_vibe_request(self, request):
        with self.conn.atomic(join=True):
            raw = None
            if request is not None:
                if request['id'] is None or request['phase'] not in recommendation_requests.VIBE_PHASES:
                    raise ValueError('Invalid Home Vibe request')
                raw = json.dumps({field: request[field] for field in ('id', 'vibe', 'phase')})
            worker_settings.save(self.conn, recommendation_requests.HOME_VIBE, raw)

    @synchronized
    def finish_vibe_request(self, request_id, phase):
        with self.conn.atomic(join=True):
            if phase not in recommendation_requests.VIBE_PHASES:
                raise ValueError('Invalid Home Vibe request phase')
            result = self.conn.execute("UPDATE settings SET value=json_set(value,'$.phase',?) "
                                       "WHERE key=? AND json_extract(value,'$.id')=?",
                                       (phase, recommendation_requests.HOME_VIBE, request_id))
            return result.rowcount == 1

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
        return tuple(choice_learning_state.refresh_input(self.conn, surface, store=self._store) or {}
                     for surface in surfaces)

    @snapshot_read
    def has_proposals(self, surface):
        """Check dependency presence without reading or decoding a large display pool."""
        return proposal_storage.present(self._store, surface)

    @snapshot_read
    def document(self, surface):
        return proposal_storage.document(self._store, surface)

