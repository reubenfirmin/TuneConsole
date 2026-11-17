"""Application wiring for analytics snapshot capture and worker publication."""
from contextlib import contextmanager
from types import SimpleNamespace

from yt_playlist.controllers.analytics_preparation import ListeningPreparation, calculate


def preparation(store):
    @contextmanager
    def snapshot():
        with store.read_snapshot() as source:
            reader = source.store
            yield SimpleNamespace(analytics=reader.listening_analytics, playlists=reader.playlists,
                settings=reader.settings, revision=source._changes, publish=source.publish)
    return ListeningPreparation(snapshot=snapshot, prepare=store.listening_analytics.prepare_generation,
                                publish=store.listening_analytics.publish_generation)


def calculate_from_reader(reader, now):
    return calculate(reader.listening_analytics, reader.playlists, reader.settings, now)
