"""Route modules for the web app, grouped by UI area.

Each module exposes ``build(ctx) -> APIRouter``; :func:`yt_playlist.web.app.create_app`
includes them all. Routers are split by UI area/concern: the cleanup page,
destructive merge/dupe operations, move, sync, the action log,
and the setup wizard.
"""
from yt_playlist.web.routes import (
    actions, album, app_update, audio_preview, charts, cleanup, clusters, collection, discovery, enrich, genres, home, likes,
    merge, move, network, playlists, radio_session, road_trip, search, setup, songs, suggest, sync, taste, track, trends,
)

# Order matters only where literal and parameterized paths share a method; the
# modules here keep those apart, so registration order is otherwise free.
MODULES = (home, suggest, cleanup, merge, playlists, charts, collection, move, sync, actions,
           setup, genres, likes, taste, album, clusters, search, network, enrich, discovery, trends,
           road_trip, songs, app_update, radio_session, track, audio_preview)


def build_all(ctx):
    return [m.build(ctx) for m in MODULES]
