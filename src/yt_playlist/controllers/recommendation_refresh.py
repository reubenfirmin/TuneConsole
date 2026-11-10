"""Recommendation refresh admission over a durable notification and fit version."""
VERSION = '1'


class RecommendationRefresh:
    def __init__(self, repository, settings):
        self.repository = repository
        self.settings = settings

    def has_pending_changes(self):
        return (self.repository.revision() is not None
                or self.settings.get_setting('rec_refresh_version') != VERSION)

    def revision(self):
        return self.repository.revision()

    def enqueue(self):
        return self.repository.enqueue()

    def publish_notification(self):
        return self.repository.publish_notification()

    def acknowledge(self, revision):
        return self.repository.acknowledge(revision)
