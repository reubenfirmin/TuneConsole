"""The identity-check resource presents injected outcomes without application storage."""
import threading
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from tests.repository_mock_support import no_database
from yt_playlist.controllers.identity_checks import IdentityCheck
from yt_playlist.web.identity_checks import build

pytestmark = pytest.mark.usefixtures('no_database')


def test_identity_checks_present_stub_outcomes_and_forward_selected_identity_in_worker_pool():
    threads = []
    outcomes = [IdentityCheck(1, 'main', 'authenticated'),
                IdentityCheck(2, 'brand', 'brand_access_denied')]
    checks = Mock(spec_set=['check'])
    checks.check.side_effect = lambda account_id: threads.append(threading.get_ident()) or outcomes
    labels = Mock(return_value=['brand'])
    app = FastAPI()
    app.include_router(build(checks, labels))

    @app.get('/event-loop')
    async def event_loop():
        return threading.get_ident()

    with TestClient(app) as client:
        response = client.get('/setup/identities/check?identity_id=2')
        assert response.status_code == 200
        data = response.json()
        assert data['auth_labels'] == ['brand']
        assert data['checks'][0] == {
            'identity_id': 1, 'label': 'main', 'state': 'valid', 'heading': 'Authenticated',
            'message': 'Authenticated.', 'help_url': None, 'help_label': None}
        assert data['checks'][1]['state'] == 'invalid'
        assert data['checks'][1]['heading'] == 'Cannot access this brand account'
        assert data['checks'][1]['help_url'] == 'https://myaccount.google.com/brandaccounts'
        assert 'owns or manages' in data['checks'][1]['message']
        checks.check.assert_called_once_with(2)
        assert threads[0] != client.get('/event-loop').json()
        labels.assert_called_once_with()
        assert client.get('/setup/identities/check?identity_id=invalid').status_code == 422
        checks.check.assert_called_once_with(2)
        client.get('/setup/identities/check')
        assert checks.check.call_args.args == (None,)
