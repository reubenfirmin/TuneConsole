"""Private SQL and real HTTP routes below ordinary rendering/action consumers."""
from importlib import import_module

import pytest

from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, mock_snapshot_coordinator,
)


@pytest.fixture(scope='module')
def route_schema():
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def route_store(tmp_path, route_schema, monkeypatch, native_sql_calls):
    """Keep SQL, files and writes; snapshot admission has independent owners."""
    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    from yt_playlist.core.db_handles import Connection
    from yt_playlist.providers import genres

    monkeypatch.setattr(DiagnosticConnection, 'cursor', Connection.cursor)
    monkeypatch.setattr(genres, 'configure', lambda *args, **kwargs: None)
    with _file_consumer_store(tmp_path, route_schema, snapshot_guards=False) as store:
        with mock_snapshot_coordinator(store, monkeypatch):
            yield store


def install_routes(monkeypatch, *names):
    """Construct the actual feature routes and shared template helpers."""
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview

    modules = [app_update, audio_preview, *(
        import_module('yt_playlist.web.routes.' + name) for name in names)]
    monkeypatch.setattr(webapp, 'build_all',
                        lambda ctx: [module.build(ctx) for module in modules])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    monkeypatch.setattr(webapp, 'BACKGROUND_WORK', False)
