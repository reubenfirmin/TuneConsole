"""Bind existing recommendation and evidence owners to suggestion commands."""
from yt_playlist.controllers.suggestions import Suggestions, SuggestionFeedback
from yt_playlist.rec import embed, graduation, measurement, rec_params, surfaces


def suggestions(store, worker):
    return Suggestions(store.playlists, store.rec_query, store.discovery, store.rec_model,
        complete=lambda pid, now: surfaces.complete_playlist(store, pid, now=now),
        neighbors=lambda *args, **kwargs: embed.neighbors_blended(store, *args, **kwargs),
        proxy=lambda *args, **kwargs: embed.neighbors_for_unmodeled(store, *args, **kwargs),
        record=lambda *args, **kwargs: measurement.record_recommendations(store, *args, **kwargs),
        rebuild=worker.trigger if worker else None)


def feedback(store):
    def facts(reason, key):
        with store.read_view() as reader:
            query = reader.rec_query
            if reason == 'vibe':
                genre = query.track_genres([key]).get(key)
                return {'family': reader.genres.feature_axes([genre])[genre][0] if genre else None}
            if reason == 'era':
                return {'decade': query.track_decades([key]).get(key)}
            if reason == 'artist':
                return {'artist': query.track_artists([key]).get(key)}
            if reason == 'mainstream':
                return {'popularity': query.track_popularity([key]).get(key),
                        'mainstream_min': rec_params.get_param(reader, 'pop_mainstream_min')}
            return {}
    return SuggestionFeedback(store.rec_model, store.rec_signals, store.playlists, store.playlist_recipes,
        facts=facts, parameter=lambda name: rec_params.get_param(store, name), lanes=rec_params.LANE_NAMES,
        facet=lambda *args, **kwargs: graduation.graduate_facet(store, *args, **kwargs),
        moods=lambda *args, **kwargs: graduation.graduate_moods(store, *args, **kwargs))
