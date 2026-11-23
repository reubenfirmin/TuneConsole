"""Move similarity links when provisional artists are reconciled."""


class ArtistState:
    def __init__(self, repository):
        self.repository = repository

    def reconcile_artist(self, previous, target, *, source):
        inputs = self.repository.reconciliation_inputs(previous, target)
        edges = [(target if row['source_artist_id'] == previous else row['source_artist_id'],
                  target if row['target_artist_id'] == previous else row['target_artist_id'], row['weight'])
                 for row in inputs]
        self.repository.apply_reconciliation(previous, target, edges)
