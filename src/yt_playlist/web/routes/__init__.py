"""Route modules for the web app, grouped by UI area.

Each module exposes ``build(ctx) -> APIRouter``; :func:`yt_playlist.web.app.create_app`
includes them all. Routers are split by the dashboard's tabs/concerns: the cleanup
dashboard, destructive merge/dupe operations, rediscover, move, sync, the action log,
and the setup wizard.
"""
from yt_playlist.web.routes import (
    actions, dashboard, merge, move, rediscover, setup, sync,
)

# Order matters only where literal and parameterized paths share a method; the
# modules here keep those apart, so registration order is otherwise free.
MODULES = (dashboard, merge, rediscover, move, sync, actions, setup)


def build_all(ctx):
    return [m.build(ctx) for m in MODULES]
