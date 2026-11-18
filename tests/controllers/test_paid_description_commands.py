"""Paid orchestration requires current admission and a single consumed send capability."""
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_descriptions import AlbumDescriptions
from yt_playlist.controllers.description_configuration import DescriptionConfiguration
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec.album_descriptions import DescriptionAccess, admit

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def ports():
    access = create_autospec(DescriptionAccess, instance=True, spec_set=True)
    requests = create_autospec(AlbumDescriptions, instance=True, spec_set=True)
    access.key.return_value = 'key'
    access.settings.return_value = {'enabled': True, 'daily_limit': 2, 'requests_today': 1}
    access.availability.return_value = {}
    requests.claim.return_value = {'status': 'reserved', 'request_id': 7, 'token': 'owner'}
    requests.authorize.return_value = True
    requests.finish.return_value = True
    return access, requests


def test_single_send_is_rechecked_after_pacing_and_capability_expires(ports):
    access, requests = ports
    sent, leaked = [], []
    def attempt(key, payload, *, now_fn, reauthorize):
        requests.authorize.assert_not_called()
        reauthorize()
        requests.authorize.assert_called_once()
        sent.append(payload)
        return {'response': True}
    def project(send):
        leaked.append(send)
        assert send({'album': 'public'}) == {'response': True}
        with pytest.raises(RuntimeError, match='exactly one'):
            send({})
        return {'status': 'ready', 'description': 'Copy', 'model': 'model'}
    # The transport invokes admission each time, including misuse by the projector.
    def transport(*args, **kwargs):
        if sent:
            kwargs['reauthorize']()
        return attempt(*args, **kwargs)
    result = admit(access, requests, 3, project, 100, policy={}, attempt=transport)
    assert result['description'] == 'Copy' and len(sent) == 1
    requests.finish.assert_called_once()
    with pytest.raises(RuntimeError, match='exactly one'):
        leaked[0]({})


def test_revoked_consent_prevents_authorization_and_post(ports):
    access, requests = ports
    access.settings.side_effect = [access.settings.return_value,
        {'enabled': False, 'daily_limit': 2, 'requests_today': 1}]
    posted = Mock()
    def transport(key, payload, *, now_fn, reauthorize):
        reauthorize()
        posted(payload)
    result = admit(access, requests, 3, lambda send: send({}), 100, policy={}, attempt=transport)
    assert result['status'] == 'paused'
    posted.assert_not_called()
    requests.authorize.assert_not_called()
    requests.finish.assert_called_once()


def test_cancellation_after_authorization_does_not_publish_or_refund(ports):
    access, requests = ports
    def transport(key, payload, *, now_fn, reauthorize):
        reauthorize()
        raise WorkClosing()
    with pytest.raises(WorkClosing):
        admit(access, requests, 3, lambda send: send({}), 100, policy={}, attempt=transport)
    requests.authorize.assert_called_once()
    requests.finish.assert_not_called()


def test_durable_busy_claim_never_reaches_projection_or_transport(ports):
    access, requests = ports
    requests.claim.return_value = {'status': 'busy', 'retry_at': 160}
    project, transport = Mock(), Mock()
    assert admit(access, requests, 3, project, 100, policy={}, attempt=transport) == {'status': 'busy', 'retry_at': 160}
    project.assert_not_called()
    transport.assert_not_called()


def test_configuration_validates_every_value_before_writes_and_wakes_after_persistence():
    settings = Mock(spec_set=['set_values'])
    notes = Mock(spec_set=['wake'])
    refresh, query = Mock(), Mock(return_value={'enabled': True})
    command = DescriptionConfiguration(settings, notes, query, now=lambda: 100, refresh=refresh)
    with pytest.raises(ValueError, match='daily request limit'):
        command.save(key='sk-validkey123', enabled=True, daily_limit='101')
    settings.set_values.assert_not_called()
    notes.wake.assert_not_called()
    notes.wake.side_effect = lambda now: settings.set_values.assert_called_once()
    assert command.save(key='sk-validkey123', enabled=True, daily_limit='2') == {'enabled': True}
    settings.set_values.assert_called_once_with({'deepseek_api_key': 'sk-validkey123',
        'deepseek_album_descriptions_enabled': '1', 'deepseek_daily_limit': '2'})
    notes.wake.assert_called_once_with(100)
    refresh.assert_called_once()


def test_configuration_disconnect_preserves_copy_and_key_only_preserves_consent_and_budget():
    settings = Mock(spec_set=['set_values'])
    notes = Mock(spec_set=['wake'])
    query, refresh = Mock(), Mock()
    command = DescriptionConfiguration(settings, notes, query, now=lambda: 100, refresh=refresh)
    command.save(disconnect=True)
    settings.set_values.assert_called_once_with({'deepseek_api_key': '', 'deepseek_album_descriptions_enabled': '0'})
    notes.wake.assert_not_called()
    refresh.assert_not_called()
    query.reset_mock()
    command.save(key='sk-newkey12345', key_only=True)
    assert settings.set_values.call_args.args[0] == {'deepseek_api_key': 'sk-newkey12345'}
    query.assert_not_called()
