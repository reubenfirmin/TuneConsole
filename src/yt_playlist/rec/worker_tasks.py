"""Small job adapters for the existing recommendation and library owners."""
class ServingTasks:
    def __init__(self, ctx, stage, *, checkpoint=None):
        self.ctx, self.stage = ctx, stage
        self.checkpoint = checkpoint

    def ratings(self):
        from yt_playlist.library import ratings
        keys = set()
        clients = self.ctx.client_provider() or {}
        for identity in self.ctx.store.accounts.get_accounts():
            keys.update(ratings.reconcile_pending(self.ctx.store, identity.id, clients.get(identity.id)))
        return keys

    def home(self):
        from yt_playlist.rec import album_choices, choice_evidence, home_prepared, measurement, permanent_taste
        from yt_playlist.library.home_album_composition import note_preparation
        now = self.ctx.now_fn()
        self.stage('recommendation policy storage', measurement.maintain_storage, self.ctx.store, now)
        # Each producer captures/finalizes its context on its own snapshot.
        # A standalone prewarm is discarded by unchanged cached producers and
        # repeats the model read on every Home admission.
        albums = self.stage("Album choices", album_choices.prepare_cached, self.ctx.store, now)
        discovery = self.stage("Home Discovery", home_prepared.prepare_discovery, self.ctx.store, now)
        revisit = self.stage("Home revisit", home_prepared.prepare_revisit, self.ctx.store, now)
        # Reporting has its own retryable publication. A failed report must not
        # prevent missing playable collections from being prepared first.
        self.stage("listening choices", choice_evidence.refresh_report, self.ctx.store, now)
        taste_pending = self.stage("permanent listener evidence", permanent_taste.prepare, self.ctx.store, now)
        notes_pending = note_preparation(self.ctx.store,
            now_playing=lambda: getattr(getattr(self.ctx, 'bridge', None), 'now_playing', None) or {},
            clients=self.ctx.client_provider, now=self.ctx.now_fn, stage=self.stage).prepare(albums, now)
        return albums is None or notes_pending or taste_pending or discovery is None or revisit is False

    def artwork(self, artist_ids):
        from yt_playlist.rec import discover
        self.stage("artist images", discover.repair_artist_images, self.ctx, artist_ids, self.ctx.now_fn())

    def tv(self):
        from yt_playlist.core.accounts import catalog_client
        from yt_playlist.rec import tv, tv_artwork
        client = catalog_client(self.ctx.client_provider())
        if client is None:
            raise ValueError('TV preparation needs a connected catalog client')
        more = self.stage('TV video pool', tv.prepare, self.ctx.store, client, self.ctx.now_fn())
        # The playable pool is already published. Artist lookups cannot hold up
        # opening the embed or change its recording/playlist identity.
        names = [item['source_artist'] for item in (tv.cached(self.ctx.store) or {}).get('items', [])]
        art_pending = self.stage('TV artist artwork', tv_artwork.prepare,
            self.ctx.store, client, names, self.ctx.now_fn())
        return more or art_pending

    def choices(self):
        from yt_playlist.rec import choice_evidence, daily_model_selection, model_selection
        now = self.ctx.now_fn()
        self.stage('model selection storage', self.ctx.store.model_selection.maintain_selection, now)
        if choice_evidence.daily_due(self.ctx.store, now):
            self.stage("daily listening choices", choice_evidence.daily, self.ctx.store, now)
        self.stage("daily model forecasts", model_selection.update_followup, self.ctx.store,
                   now, now_fn=self.ctx.now_fn)
        return self.stage('daily model decision', daily_model_selection.maintain, self.ctx.store, now,
                   checkpoint=self.checkpoint)

    def stories(self):
        from yt_playlist.rec import into_recently
        return self.stage("recent listening stories", into_recently.prepare, self.ctx.store, self.ctx.now_fn())

    def stories_due(self):
        from yt_playlist.rec import into_recently
        return self.stage("recent story refresh check", into_recently.refresh_due, self.ctx.store, self.ctx.now_fn())
