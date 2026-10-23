"""Small job adapters for the existing recommendation and library owners."""


class ServingTasks:
    def __init__(self, ctx, stage):
        self.ctx, self.stage = ctx, stage

    def ratings(self):
        from yt_playlist.library import ratings
        keys = set()
        clients = self.ctx.client_provider() or {}
        for identity in self.ctx.store.identities.get_identities():
            keys.update(ratings.reconcile_pending(self.ctx.store, identity.id, clients.get(identity.id)))
        return keys

    def home(self):
        from yt_playlist.rec import graduation, home_prepared
        result = self.stage("Home collections", home_prepared.prepare, self.ctx.store, self.ctx.now_fn())
        if result is None:
            self.ctx.logger.debug("Home preparation deferred; retrying after preparation backoff")
            return True
        self.stage("held steering exposure", graduation.graduate_slider_exposure, self.ctx.store, self.ctx.now_fn())
        self.stage("listening exposure", graduation.graduate_play_exposure,
                   self.ctx.store, self.ctx.now_fn(), prepared_leans=result["play_exposure"])
        target = home_prepared.MAX_MENUS if result.get('modes') else 1
        return len(result["menus"]) < target

    def artwork(self, artist_ids):
        from yt_playlist.rec import discover
        self.stage("artist images", discover.repair_artist_images, self.ctx, artist_ids, self.ctx.now_fn())

    def stories(self):
        from yt_playlist.rec import into_recently
        return self.stage("recent listening stories", into_recently.prepare, self.ctx.store, self.ctx.now_fn())
