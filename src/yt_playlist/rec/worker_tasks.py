"""Small job adapters for the existing recommendation and library owners."""


class ServingTasks:
    def __init__(self, ctx, stage, *, checkpoint=None):
        self.ctx, self.stage = ctx, stage
        self.checkpoint = checkpoint
        self._home_requested = self._home_prepared = 0

    def request_home_refresh(self):
        # Called under the worker lock. A request arriving during preparation
        # must survive that publication, as must a deferred explicit attempt.
        self._home_requested += 1

    def ratings(self):
        from yt_playlist.library import ratings
        keys = set()
        clients = self.ctx.client_provider() or {}
        for identity in self.ctx.store.accounts.get_accounts():
            keys.update(ratings.reconcile_pending(self.ctx.store, identity.id, clients.get(identity.id)))
        return keys

    def home(self):
        from yt_playlist.rec import album_choices, album_notes, album_tuning, choice_evidence, graduation, home_prepared, measurement
        now = self.ctx.now_fn()
        requested = self._home_requested
        prepare_mixes = (requested != self._home_prepared
                         or not home_prepared.has_prepared_batch(self.ctx.store, now))
        self.stage('recommendation policy storage', measurement.maintain_storage, self.ctx.store, now)
        # Each producer captures/finalizes its context on its own snapshot.
        # A standalone prewarm is discarded by unchanged cached producers and
        # repeats the model read on every Home admission.
        albums = self.stage("Album choices", album_choices.prepare_cached, self.ctx.store, now)
        result = (self.stage("Home collections", home_prepared.prepare_mixes, self.ctx.store, now)
                  if prepare_mixes else None)
        if result is not None:
            self._home_prepared = requested
        discovery = self.stage("Home Discovery", home_prepared.prepare_discovery, self.ctx.store, now)
        revisit = self.stage("Home revisit", home_prepared.prepare_revisit, self.ctx.store, now)
        # Reporting has its own retryable publication. A failed report must not
        # prevent missing playable collections from being prepared first.
        self.stage("listening choices", choice_evidence.refresh_report, self.ctx.store, now)
        tuning = album_tuning.state(self.ctx.store, now)
        from yt_playlist.rec.home_cards import _epoch
        epoch = _epoch(self.ctx.store, 'album_choices')
        excluded = album_choices.excluded_from_refresh(self.ctx.store, epoch)
        playing = getattr(getattr(self.ctx, 'bridge', None), 'now_playing', None) or {}
        current = album_choices.current_browse_id(self.ctx.store, playing)
        _, menu, _ = album_choices.selection(self.ctx.store, (albums or {}).get('items', []), now,
                                            tuning['values'], epoch, tuning['genres'],
                                            exclude=excluded, current=current)
        from yt_playlist.core.accounts import catalog_client
        notes_pending = self.stage("Album sleeve notes", album_notes.prepare, self.ctx.store, now, menu,
                                   client=catalog_client(self.ctx.client_provider()), now_fn=self.ctx.now_fn)
        if prepare_mixes and result is None:
            self.ctx.logger.debug("Home preparation deferred; retrying after preparation backoff")
            return True
        if result is not None:
            self.stage("held steering exposure", graduation.graduate_slider_exposure, self.ctx.store, self.ctx.now_fn())
            self.stage("listening exposure", graduation.graduate_play_exposure,
                       self.ctx.store, self.ctx.now_fn(), prepared_leans=result["play_exposure"])
        return (albums is None or notes_pending or discovery is None or revisit is False
                or self._home_requested != self._home_prepared
                or prepare_mixes and not home_prepared.batch_complete(result))

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
        self.stage('model comparison storage', self.ctx.store.model_selection.maintain_selection, now)
        if choice_evidence.daily_due(self.ctx.store, now):
            self.stage("daily listening choices", choice_evidence.daily, self.ctx.store, now)
        self.stage("daily model forecasts", model_selection.update_followup, self.ctx.store,
                   now, now_fn=self.ctx.now_fn)
        return self.stage('daily model decision', daily_model_selection.maintain, self.ctx.store, now,
                   checkpoint=self.checkpoint)

    def stories(self):
        from yt_playlist.rec import into_recently
        return self.stage("recent listening stories", into_recently.prepare, self.ctx.store, self.ctx.now_fn())
