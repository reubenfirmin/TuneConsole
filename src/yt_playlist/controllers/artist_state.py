"""Choose current similarity state when provisional artists are reconciled."""
from yt_playlist.core import artist_reconciliation as policy


class ArtistState:
    def __init__(self, repository):
        self.repository = repository

    def reconcile_artist(self, previous, target, *, source):
        inputs = self.repository.reconciliation_inputs(previous, target)
        winner = policy.latest(inputs['caches'], 'fetched_at', target, legacy=True)
        edges = [(target if row['source_artist_id'] == previous else row['source_artist_id'],
                  target if row['target_artist_id'] == previous else row['target_artist_id'], row['weight'])
                 for row in inputs['edges']]
        self.repository.apply_reconciliation(previous, target, winner, edges)
