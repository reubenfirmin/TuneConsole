"""Exercise real prose rules without dispatch and a paid command with bounded ports."""
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from tests.providers.test_deepseek import ALBUM, TEXT, response
from yt_playlist.controllers.album_descriptions import AlbumDescriptions
from yt_playlist.controllers.description_access import DescriptionAccess
from yt_playlist.integrations.deepseek import description_response
from yt_playlist.providers import deepseek
from yt_playlist.rec.album_descriptions import admit

pytestmark = pytest.mark.usefixtures('no_database')


def test_request_and_projection_are_detached_and_preserve_public_provenance():
    request = deepseek.description_request(ALBUM)
    assert json.loads(request['messages'][1]['content']) == {key: ALBUM[key] for key in ('artist', 'title', 'year')}
    document, usage = description_response(response(), deepseek.MODEL)
    projected = deepseek.project_description(ALBUM, document, usage)
    assert projected['description'] == TEXT and projected['usage']['total_tokens'] == 319
    assert projected['album'] == deepseek.album_fields(ALBUM)
    assert 'browse_id' not in projected['album'] and projected['prompt_version'] == deepseek.PROMPT_VERSION


@pytest.mark.parametrize('changes', [dict(confidence='low'), dict(description=''), dict(description=None)])
def test_abstention_uses_supplied_document(changes):
    assert deepseek.project_description(ALBUM, dict(description=TEXT, confidence='high') | changes, {})['description'] is None


@pytest.mark.parametrize('changes', [dict(confidence='certain'), dict(description='Too short'),
    dict(description=TEXT + ' https://example.com'), dict(description=TEXT + ' <em>markup</em>'),
    dict(uncertainty=['not prose']), dict(description=4)])
def test_projection_rejects_invalid_prose_without_executable_dependencies(changes):
    with pytest.raises(deepseek.DescriptionUnavailable, match='invalid_response'):
        deepseek.project_description(ALBUM, dict(description=TEXT, confidence='moderate') | changes, {})


def test_real_request_and_projector_run_inside_one_admitted_paid_command():
    access = create_autospec(DescriptionAccess, instance=True, spec_set=True)
    requests = create_autospec(AlbumDescriptions, instance=True, spec_set=True)
    access.key.return_value = 'key'
    access.settings.return_value = dict(enabled=True, daily_limit=2, requests_today=1)
    access.availability.return_value = {}
    requests.claim.return_value = dict(status='reserved', request_id=3, token='owner')
    requests.authorize.return_value = requests.finish.return_value = True
    posts = []
    def attempt(key, payload, *, now_fn, reauthorize):
        reauthorize()
        requests.authorize.assert_called_once()
        posts.append(payload)
        return response()
    def project(send):
        document, usage = description_response(send(deepseek.description_request(ALBUM)), deepseek.MODEL)
        return dict(deepseek.project_description(ALBUM, document, usage), at=100, status='ready')
    result = admit(access, requests, 2, project, 100, policy=deepseek.description_policy(), attempt=attempt)
    assert result['description'] == TEXT and len(posts) == 1
    requests.finish.assert_called_once_with(3, 'owner', result, 100)
