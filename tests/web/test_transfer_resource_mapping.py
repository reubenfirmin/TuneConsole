"""Transfer resources decode forms and map neutral command rejections."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.repository_mock_support import no_database
from yt_playlist.controllers.spotify_transfer import TransferRejected
from yt_playlist.web.routes import setup


@pytest.mark.parametrize('reason,status', [('busy', 409), ('expired', 410), ('invalid', 400)])
def test_prepare_maps_command_rejection_without_running_workflow(no_database, monkeypatch, reason, status):
    transfer = Mock(spec_set=['prepare'])
    transfer.prepare.side_effect = TransferRejected(reason, 'Selection unavailable')
    monkeypatch.setattr(setup, 'transfer_commands', lambda ctx: transfer)
    monkeypatch.setattr(setup, 'HistoryImports', lambda ctx: None)
    store = NS(accounts=Mock(), settings=Mock(), genres=Mock(), album_notes=Mock())
    ctx = NS(store=store, templates=Mock(), setup=None, clear_auth_expired=Mock(), auth_expired={},
             now_fn=lambda: 100, rec_worker=None, enrich_worker=None)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    with TestClient(app) as client:
        response = client.post('/import/spotify-library/prepare', data={
            'selection_token': 'source', 'identity_id': '7', 'playlist_index': ['2', '0'], 'include_albums': 'on'})
    assert response.status_code == status
    assert response.json() == {'detail': 'Selection unavailable'}
    transfer.prepare.assert_called_once_with('source', '7', ['2', '0'], True)
