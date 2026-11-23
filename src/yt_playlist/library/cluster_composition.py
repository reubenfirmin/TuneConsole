"""Bind bounded snapshot queries to the cluster application command."""
from yt_playlist.controllers.clusters import ClusterQuery
from yt_playlist.rec import cluster_ranking, embed


def query(reader):
    return ClusterQuery(reader.rec_query, reader.discovery, reader.genres, reader.cluster_evidence,
        rank_candidates=lambda **kwargs: cluster_ranking.rank_candidates(reader, **kwargs),
        geometry=lambda *args: embed.connection_geometry(reader, *args))


def expansion(reader, body):
    return query(reader).expansion(body)


def explanation(reader, body):
    return query(reader).explanation(body)
