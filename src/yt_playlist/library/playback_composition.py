"""Bind the local playback projection to installed persistence capabilities."""


def observations(store, worker=None, *, in_projection=False):
    from yt_playlist.controllers.playback_observations import PlaybackObservations
    from yt_playlist.core import playback_inbox
    from yt_playlist.library.choice_composition import playback
    from yt_playlist.library import rating_projection
    from yt_playlist.rec import listening_analytics
    choices = playback(store, in_projection=in_projection)
    return PlaybackObservations(store.accounts, store.tracks, store.history, store.settings, store.player_events,
        deferred=lambda: playback_inbox.deferred(store),
        capture=lambda *args: playback_inbox.capture(store, *args),
        album_context=choices.album_context, record_playback=choices.record, attributed=choices.attributed,
        capture_start=lambda *args, **kwargs: listening_analytics.capture_start(store, *args, **kwargs),
        observe_exit=choices.observe, observe_like=choices.observe_like,
        rating_observations=lambda *args, **kwargs: rating_projection.record_observations(store, *args, **kwargs),
        confirmed_rating=lambda *args, **kwargs: rating_projection.record_confirmed(store, *args, **kwargs),
        reconcile_ratings=lambda *args: rating_projection.reconcile_recordings(store, *args),
        refresh_ratings=worker.trigger_rating_update if worker else lambda: None)
