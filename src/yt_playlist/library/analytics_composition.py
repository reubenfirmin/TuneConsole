"""Bind analytics application ports; resources never acquire report inputs."""
from contextlib import contextmanager
from types import SimpleNamespace

from yt_playlist.controllers.analytics_queries import AnalyticsQueries, StoryQueries
from yt_playlist.rec.discover import fetch_artist_info


@contextmanager
def chart_reader(store):
    with store.read_view() as reader:
        yield SimpleNamespace(charts=reader.charts, playlists=reader.playlists, collection=reader.collection)


def queries(ctx):
    store = ctx.store
    return AnalyticsQueries(reader=lambda: chart_reader(store), listening=store.listening_analytics,
        artists=store.artists, collection=store.collection,
        provider=lambda name, browse: fetch_artist_info(ctx, name, browse), settings=store.settings,
        capture=lambda: getattr(store, 'choice_capture', None), now=ctx.now_fn,
        refresh_analytics=lambda: ctx.rec_worker.trigger_analytics_refresh() if ctx.rec_worker else None)


def stories(ctx):
    worker = getattr(ctx, 'rec_worker', None)
    return StoryQueries(proposals=ctx.store.rec_proposals, settings=ctx.store.settings,
        refresh=lambda: worker.trigger_trends_refresh() if worker else None,
        paused=lambda: getattr(ctx.store, 'activity_paused', False))
