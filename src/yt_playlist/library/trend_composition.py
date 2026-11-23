"""Bind recap preparation to the existing private snapshot and SQL publication owners."""
from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace

from yt_playlist.controllers import trend_preparation
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.util import genre_family_data


def capture_family_adjacency():
    _, adjacency, _, _ = genre_family_data.load()
    return MappingProxyType({family: MappingProxyType(dict(neighbors))
                             for family, neighbors in adjacency.items()})


def inputs(reader):
    return SimpleNamespace(trends=reader.trends, settings=reader.settings, genres=reader.genres,
                           charts=reader.charts, recap=reader.recap, musical_profiles=reader.musical_profiles,
                           family_adjacency=capture_family_adjacency())


def preparation(store):
    @contextmanager
    def snapshot():
        with (store.read_snapshot() as source,
              column_group_reads(source.store, 'recordings', 'catalog'),
              column_group_reads(source.store, 'playlists', 'recap'),
              column_group_reads(source.store, 'playlist_scopes', 'recap')):
            facts = inputs(source.store)
            facts.is_current, facts.publish_ordered = source.is_current, source.publish_ordered
            yield facts
    return trend_preparation.TrendPreparation(snapshot=snapshot, publish=store.trends.publish_rollup)


def calculate(reader, now):
    return trend_preparation.calculate(inputs(reader), now)


def review(reader, months, now, day_counts=(), meta=None):
    return trend_preparation.review(inputs(reader), months, now, day_counts, meta)


def assemble(reader, now, day_counts, meta, months):
    return trend_preparation.assemble(inputs(reader), now, day_counts, meta, months)
