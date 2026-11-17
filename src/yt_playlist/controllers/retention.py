"""Coordinate bounded storage maintenance and file-protection lifetimes."""


class Retention:
    def __init__(self, database, enrichment, models, embeddings, profiles, predictions, managed, collections):
        self.database = database
        self.enrichment, self.models = enrichment, models
        self.embeddings = embeddings
        self.profiles = profiles
        self.predictions = predictions
        self.managed = managed
        self.collections = collections
        self._lock = database._lock

    @property
    def conn(self):
        return self.database.conn

    # Each established owner controls its own writer admission, file fences and
    # iterator lifetime. Do not hold the serving lock across a complete turn.
    def maintain_managed_files(self, now):
        self.enrichment.expire_provider_alternatives(now)
        return self.managed.maintain(now)

    def maintain_prediction_provenance(self, now):
        return self.predictions.maintain(now)

    def collect_musical_profiles(self, now):
        return self.profiles.collect(now)

    def collect_embeddings(self, now):
        return self.embeddings.collect(now)

    def maintain_runtime_history(self, now):
        return self.managed.repository.maintain_history(now)

    def maintain_collection_recovery(self):
        return self.collections.maintain()

    def collection_recovery_pending(self):
        return self.collections.pending()

    def close(self):
        """Release disposable directory iterators after the worker has stopped."""
        self.managed.close()
        self.collections.close()
