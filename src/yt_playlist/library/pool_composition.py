"""Bind pool acquisition and guarded publication to the library."""
from contextlib import contextmanager
from types import SimpleNamespace

from yt_playlist.controllers.pool_preparation import FreshAcquisition, LibraryPoolPreparation
from yt_playlist.rec import embedding_vectors, home_themes, home_vibe, library_pool_inputs as library_pool, measurement, road_trip_candidates
from yt_playlist.library import mode_composition


def builds(ctx):
    from yt_playlist.controllers.pool_preparation import PoolBuilds
    from yt_playlist.core.accounts import catalog_client
    return PoolBuilds(client=lambda: catalog_client(ctx.client_provider()),
        populate=lambda client, now, include_genres=(), avoid_genres=(), on_progress=lambda text: None:
            fresh(ctx.store).populate(client, now, include_genres, avoid_genres, on_progress),
        prepare_fresh=lambda now: mode_composition.scheduled(ctx.store, now), head=lambda surface: ctx.store.recommendations.head(surface),
        prepare_library=lambda now, parameters: library(ctx.store).prepare(now, parameters),
        refresh_candidates=lambda: ctx.rec_worker.trigger_candidate_refresh(),
        refresh_home=lambda **kwargs: ctx.rec_worker.trigger_home_refresh(**kwargs), now=ctx.now_fn, logger=ctx.logger)


def fresh(store):
    return FreshAcquisition(store.rec_query, store.discovery, store.rec_model, store.genres, store.tracks,
        options=lambda: home_themes.genre_options(store),
        artists=lambda genre, **kwargs: road_trip_candidates.genre_artists(store, genre, **kwargs),
        songs=lambda client, artist, limit: road_trip_candidates.other_input_songs(client, 'artist', artist, limit, store),
        rebuild=lambda: embedding_vectors.build_discovered_content_vectors(store))


def library(store):
    @contextmanager
    def snapshot():
        with store.read_snapshot() as captured:
            reader = captured.store
            yield SimpleNamespace(calculate=lambda now, parameters: library_pool._calculate(reader, now, parameters),
                policies=lambda now: measurement.PoolPolicies(reader, now), recommendations=reader.recommendations,
                serial=reader.prepared_content.serial, release=captured.release_reader, publish=captured.publish_ordered)

    return LibraryPoolPreparation(store.rec_ledger, store.recommendations,
        vibe=lambda now: home_vibe.current(store, now), scheduled=lambda now: mode_composition.scheduled(store, now),
        snapshot=snapshot)
