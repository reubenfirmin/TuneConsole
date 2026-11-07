"""Plan group retirement on a private reader, then guard its bounded SQL write."""
from yt_playlist.core import group_retention_policy
from yt_playlist.repos import group_publication


class GroupRetention:
    def __init__(self, repository, snapshots):
        self.repository, self.snapshots = repository, snapshots

    def step(self):
        with self.snapshots() as snapshot:
            selected = group_retention_policy.plan(snapshot.store.group_retention,
                                                    batch=group_publication.BATCH_ROWS)
            if selected is None:
                return False
            snapshot.release_reader()
            return snapshot.publish(lambda: self.repository.apply(selected))
