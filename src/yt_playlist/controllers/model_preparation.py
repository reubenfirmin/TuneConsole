"""Own model preparation and guarded publication, independently of worker scheduling."""
from yt_playlist.core.db_revisions import column_group_reads


class ModelPreparation:
    def __init__(self, *, snapshot, refresh, model, settings, modes, prepare):
        self.snapshot, self.refresh, self.model = snapshot, refresh, model
        self.settings, self.modes, self.prepare = settings, modes, prepare

    def rebuild(self, now, **options):
        revision = self.refresh.revision()
        with self.snapshot() as snapshot:
            # Provider scheduling stamps cannot invalidate a catalog-only fit.
            with column_group_reads(snapshot.store, 'recordings', 'catalog'):
                prepared = self.prepare(snapshot.store, now, **options)
            changes = snapshot.store.rec_model.prepare_training_changes(prepared['vectors'], prepared['settings'])
            prepared['vector_changes'] = changes

            def publish():
                self.model.publish_training_generation(changes)
                self.settings.publish_values(prepared['settings'])
                if prepared['modes'] is not None:
                    self.modes.publish_generation(*prepared['modes'])
                self.refresh.acknowledge(revision)

            return prepared if snapshot.publish_ordered(publish) else None
