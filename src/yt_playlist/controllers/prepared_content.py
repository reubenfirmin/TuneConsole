"""Content preparation over catalog facts and discovery inputs."""
import json

from yt_playlist.core import prepared_inputs
from yt_playlist.rec import prepared_content_inputs


class PreparedContent:
    def __init__(self, repository, discovery, refresh):
        self.repository = repository
        self.discovery = discovery
        self.refresh = refresh

    def capture(self, pending, *, mask_numeric=False):
        pending = list(pending)
        keys = [key for key, _revision in pending]
        if not keys:
            return []
        with self.repository.capture_reads(keys):
            facts = self.repository.capture_facts(keys, mask_numeric=mask_numeric)
            discovered = self.discovery.discovered_tracks_by_keys(keys, metadata_only=mask_numeric)
        return prepared_content_inputs.capture(pending, facts, discovered)

    def pending(self, limit=32):
        return self.repository.pending(limit)

    def requeue_recording(self, recording_id):
        return self.repository.requeue_recording(recording_id)

    def has_pending(self):
        return self.repository.has_pending()

    def counters(self):
        return self.repository.counters()

    def progress(self):
        return self.repository.progress()

    def changed_since(self, revision):
        return self.repository.changed_since(revision)

    def publish_capture(self, rows, now):
        rows = list(rows)
        values = []
        for key, _revision, raw, digest, different in rows:
            if different:
                payload = json.loads(raw)
                values.append((key, prepared_inputs.flatten(payload), digest, not any(payload.values())))
        # Notify before accepting a new digest. A retry after a later failure
        # may find that digest already installed. Acknowledge the batch last.
        if values:
            self.refresh.enqueue()
        changed = self.repository.publish_capture(values, now)
        self.repository.acknowledge([(key, revision) for key, revision, *_ in rows])
        return changed

    def rotate(self, pending):
        return self.repository.rotate(pending)

    def corpus(self, limit, *, max_bytes=None):
        return self.repository.corpus(limit, max_bytes=max_bytes)

    def missing_previous_inputs(self):
        return self.repository.missing_previous_inputs()

    def missing_unqueued(self, limit=32, *, after=None):
        return self.repository.missing_unqueued(limit, after=after)

    def enqueue_missing(self, keys):
        return self.repository.enqueue_missing(keys)

    def vector_storage_bytes(self):
        return self.repository.vector_storage_bytes()

    def serial(self):
        return self.repository.serial()
