"""Bind existing recommendation and evidence owners to suggestion commands."""
from yt_playlist.controllers.suggestions import Suggestions, SuggestionFeedback
from yt_playlist.core.accounts import catalog_client
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.library import artist_neighbors
from yt_playlist.rec import embed, measurement, rec_params, surfaces


def suggestions(store, worker, clients=None):
    def complete(pid, now):
        youtube = {}
        if clients is not None and (client := catalog_client(clients())) is not None:
            members = store.playlists.get_playlist_track_keys(pid)
            refs = store.artists.track_keys(keys=members)
            seeds = sorted({artist for artists in refs.values() for artist in artists})
            references = ((seed, store.artists.get_artist(seed)) for seed in seeds)
            available = [(seed, ref) for seed, ref in references if ref and ref['provider_ids'].get('youtube')]
            try:
                with work_budget(3):
                    for seed, ref in available[:4]:
                        youtube[seed] = artist_neighbors.youtube(client, ref)
            except WorkBudgetExceeded:
                pass  # Use the supplemental pages that arrived within the read budget.
        return surfaces.complete_playlist(store, pid, now=now, youtube=youtube)

    return Suggestions(store.playlists, store.rec_query, store.discovery, store.rec_model,
        complete=complete,
        neighbors=lambda key, *, topn: embed.track_neighbors(store, key, topn=topn),
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
        facts=facts, parameter=lambda name: rec_params.get_param(store, name), lanes=rec_params.LANE_NAMES)
