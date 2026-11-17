"""Update transport mapping with an injected command and no application database."""
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from tests.repository_mock_support import no_database
from yt_playlist.web.routes.app_update import build

pytestmark = pytest.mark.usefixtures('no_database')


def test_update_routes_use_injected_commands_and_preserve_transport_admission():
    commands = Mock(spec_set=['available', 'status', 'install', 'restart', 'cancel'])
    commands.available = True
    commands.status = AsyncMock(return_value={'phase': 'ready'})
    commands.install = AsyncMock(return_value=({'phase': 'installing'}, 202))
    commands.restart.return_value = ({'phase': 'waiting'}, 202)
    commands.cancel = AsyncMock(return_value=({'phase': None}, 200))
    app = FastAPI()
    app.include_router(build(None, commands=commands))
    with TestClient(app, base_url='http://127.0.0.1:9123') as client:
        assert client.get('/app-update/status').json() == {'phase': 'ready'}
        denied = client.post('/app-update/install', headers={'Origin': 'http://127.0.0.1:9000'})
        assert denied.status_code == 403
        commands.install.assert_not_awaited()
        installed = client.post('/app-update/install')
        assert installed.status_code == 202 and installed.json() == {'phase': 'installing'}
        assert installed.headers['cache-control'] == 'no-store'
        assert client.post('/app-update/restart').status_code == 202
        commands.restart.assert_called_once_with('127.0.0.1', 9123)
        assert client.post('/app-update/restart/cancel').json() == {'phase': None}
        commands.restart.side_effect = ValueError('Install the update first')
        assert client.post('/app-update/restart').status_code == 409
        commands.available = False
        assert client.get('/app-update/status').status_code == 404
