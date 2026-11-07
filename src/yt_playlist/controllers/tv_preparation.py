"""Publish TV preparation outcomes through their installed storage owner."""
from yt_playlist.core import recommendation_work


class TVPreparation:
    def __init__(self, repository, proposals):
        self.repository = repository
        self.proposals = proposals

    def state(self):
        if self.repository.available():
            return self.repository.state()
        return self.proposals.get_proposals('tv_preparation')

    def record(self, failed, now):
        value = {'failed': failed, 'at': now}
        row = recommendation_work.parse('tv_preparation', value, now)
        if self.repository.available():
            self.repository.record(value, row, now)
        else:
            self.proposals.put_proposals('tv_preparation', value, now)
