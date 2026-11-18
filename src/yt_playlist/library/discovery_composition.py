"""Bind discovery workflows to local facts and authorized provider access."""
from yt_playlist.controllers.discovery_preparation import DiscoveryArtists, DiscoveryPreparation
from yt_playlist.providers import lastfm, genres
from yt_playlist.rec import artist_model, artist_bridges, embedding_vectors, rec_params
from yt_playlist.core.accounts import catalog_client
from yt_playlist.core.activity import WorkClosing
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.rec.scoring import playlist_taste
from yt_playlist.util import net


def catalog(ctx):
    from yt_playlist.controllers.discovery_catalog import DiscoveryCatalog
    from yt_playlist.core.accounts import catalog_client, main_client
    import time
    store = ctx.store
    return DiscoveryCatalog(catalog=lambda: catalog_client(ctx.client_provider()),
        radio_client=lambda: main_client(store, ctx.client_provider()), collection=store.collection,
        discovery=store.discovery, query=store.rec_query, tracks=store.tracks, charts=store.charts,
        settings=store.settings, rebuild=lambda: embedding_vectors.build_discovered_content_vectors(store),
        now=getattr(ctx, 'now_fn', time.time), logger=ctx.logger)


def artists(ctx):
    store = ctx.store
    key = lastfm.api_key(store)
    matcher = None

    def anchors(values, index, limit):
        return artist_bridges.anchor_values(values, index, store.rec_query.top_played_keys(limit=12),
            store.artists.track_keys(library=True), [(store.artists.key(row.get('artist_id') or row['artist']), row['plays'])
                                                    for row in store.charts.top_artists(limit)])

    def thumbnail(name):
        try:
            client = catalog_client(ctx.client_provider())
            rows = client.search(name, filter='artists') or [] if client else []
            return best_thumb(rows[0].get('thumbnails')) if rows else None
        except WorkClosing:
            raise
        except Exception:
            return None

    def similar(name):
        with lastfm.access(store), net.probe_errors() as errors:
            rows = [[name, match] for name, match in lastfm.similar_artists(name, key)]
        for error in errors:
            net.record_probe_failure(error)
        return rows, not errors

    def genre(name):
        nonlocal matcher
        if matcher is None:
            matcher = genres.snapshot(store)
        with lastfm.access(store):
            return lastfm.artist_genre(name, key, matcher=matcher)

    return DiscoveryArtists(taste=lambda: playlist_taste(store) if key else [],
        vectors=lambda: embedding_vectors.load_vectors(store),
        anchors=anchors,
        owned=store.rec_query.library_artists, reference=store.artists.get_artist,
        cache=store.artist_similarity, similar=similar,
        neighbors=lambda name, **kw: artist_model.artist_neighbors(store, name, **kw),
        rank=artist_bridges.rank_candidates, thumbnail=thumbnail,
        genre=genre, now=ctx.now_fn)


def preparation(store, *, ctx=None):
    return DiscoveryPreparation(store.rec_query, store.collection, store.artists, store.discovery,
        store.rec_eligibility, store.history, parameter=lambda name: rec_params.get_param(store, name),
        artist_info=lambda name, browse: catalog(ctx).artist(name, browse),
        new_artists=lambda: artists(ctx).query(15, 30),
        populate=lambda now: catalog(ctx).populate(now, 4),
        radio=lambda now: catalog(ctx).radio(now, 6, 12), logger=getattr(ctx, 'logger', None))
