"""Bind the local playback projection to installed persistence capabilities."""


def observations(store, worker=None):
    from yt_playlist.controllers.playback_observations import PlaybackObservations
    from yt_playlist.core import playback_inbox
    from yt_playlist.library import rating_projection
    from yt_playlist.rec import choice_evidence, choice_outcomes, listening_analytics
    return PlaybackObservations(store.accounts, store.tracks, store.history, store.settings, store.player_events,
        deferred=lambda: playback_inbox.deferred(store),
        capture=lambda *args: playback_inbox.capture(store, *args),
        album_context=lambda *args, **kwargs: choice_evidence.album_playback_context(store, *args, **kwargs),
        record_playback=lambda *args, **kwargs: choice_evidence.record_playback(store, *args, **kwargs),
        capture_start=lambda *args, **kwargs: listening_analytics.capture_start(store, *args, **kwargs),
        observe_exit=lambda *args: choice_outcomes.observe(store, *args),
        rating_observations=lambda *args, **kwargs: rating_projection.record_observations(store, *args, **kwargs),
        confirmed_rating=lambda *args: rating_projection.record_confirmed(store, *args),
        reconcile_ratings=lambda *args: rating_projection.reconcile_recordings(store, *args),
        refresh_ratings=worker.trigger_rating_update if worker else lambda: None)
