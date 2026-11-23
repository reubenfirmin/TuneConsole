"""Recommendation refresh admission over a durable notification and fit version."""
VERSION = '3'


class RecommendationRefresh:
    def __init__(self, repository):
        self.repository = repository

    def has_pending_changes(self):
        return self.repository.revision() is not None or self.version() != VERSION

    def version(self):
        return self.repository.version()

    def complete_fit(self, revision):
        self.repository.complete_fit(VERSION, revision)

    def clear_fit(self):
        self.repository.save_version(None)

    def revision(self):
        return self.repository.revision()

    def enqueue(self):
        return self.repository.enqueue()

    def publish_notification(self):
        return self.repository.publish_notification()

    def acknowledge(self, revision):
        return self.repository.acknowledge(revision)
