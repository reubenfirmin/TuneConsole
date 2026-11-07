"""Coordinate bounded storage maintenance and file-protection lifetimes."""


class Retention:
    def __init__(self, database, enrichment, models):
        self.database = database
        self.enrichment, self.models = enrichment, models
        self._lock = database._lock

    @property
    def conn(self):
        return self.database.conn

    # Each established owner controls its own writer admission, file fences and
    # iterator lifetime. Do not hold the serving lock across a complete turn.
    def maintain_managed_files(self, now):
        from yt_playlist.core.preparation import managed_files
        self.enrichment.expire_provider_alternatives(now)
        return managed_files.maintain(self.database, now)

    def maintain_prediction_provenance(self, now):
        from yt_playlist.core import vibe_provenance, vibe_history
        from yt_playlist.core.db_writes import atomic_write
        from yt_playlist.repos import vibe_models
        with self._lock:
            if not vibe_provenance.available(self.conn):
                return 0
        with atomic_write(self.database):
            expired = vibe_provenance.maintain(self.conn, now)
        retired = vibe_history.maintain(self.database, now)
        with self._lock:
            vibe_provenance.collect(self.conn, now)
            if vibe_models.available(self.conn):
                self.models.collect(now)
        return expired + retired

    def collect_musical_profiles(self, now):
        from yt_playlist.core import profile_files
        with self._lock:
            if not profile_files.available(self.conn):
                return 0
            return profile_files.collect(self.conn, now)

    def collect_embeddings(self, now):
        from yt_playlist.core.preparation import embeddings
        return embeddings.maintain(self.database, now)

    def expire_legacy_diagnostics(self, now):
        from yt_playlist.core.preparation import legacy_journals
        return legacy_journals.maintain(self.database, now)

    def maintain_runtime_history(self, now):
        from yt_playlist.core import credit_retention, mode_retention
        from yt_playlist.core.db_writes import atomic_write
        progress = {}
        for domain, owner in (('credits', credit_retention), ('modes', mode_retention)):
            with atomic_write(self.database):
                progress[domain] = owner.step(self.conn, now, activate=False)
        return progress

    def maintain_collection_recovery(self):
        from yt_playlist.core import collection_recovery
        return collection_recovery.maintain(self.database)

    def collection_recovery_pending(self):
        from yt_playlist.core import collection_recovery
        return collection_recovery.pending(self.database)

    def close(self):
        """Release disposable directory iterators after the worker has stopped."""
        from yt_playlist.core import collection_recovery
        from yt_playlist.core.preparation import managed_files
        managed_files.close(self.database)
        collection_recovery.close(self.database)
