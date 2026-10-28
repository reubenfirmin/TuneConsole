import io
import json
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import pytest

from yt_playlist.providers import deepseek
from yt_playlist.integrations.deepseek import DeepSeekIAO

ALBUM = {'artist': 'Patrice Bäumel', 'title': 'Global Underground #42: Berlin (DJ Mix)',
         'year': '2019', 'browse_id': 'private-local-id', 'plays': 99, 'taste': {'private': True}}
TEXT = (
    'The mix draws on melodic techno and deep, atmospheric house, built around long, patient '
    'transitions and a moody, hypnotic arc rather than big drops. Its Berlin framing points '
    "to the city's club culture, and the selection leans toward brooding, textured tracks that "
    'reward close listening over its runtime.'
)


def response(**changes):
    return {'model': deepseek.MODEL, 'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps({'description': TEXT, 'confidence': 'moderate',
                               'uncertainty': 'Specific running order is uncertain.', **changes})}}],
        'usage': {'prompt_tokens': 167, 'completion_tokens': 152, 'total_tokens': 319}}


def test_flash_request_is_bounded_and_sends_only_public_album_identity(monkeypatch):
    sent = []
    result = deepseek.describe(ALBUM, lambda payload: sent.append(payload) or response())
    payload, = sent
    assert payload['model'] == 'deepseek-flash' and payload['thinking'] == {'type': 'disabled'}
    assert payload['max_tokens'] == 400
    assert json.loads(payload['messages'][1]['content']) == {
        'artist': ALBUM['artist'], 'title': ALBUM['title'], 'year': '2019'}
    assert all(private not in json.dumps(payload) for private in ('private-local-id', 'taste', '99'))
    assert result['description'] == TEXT and result['usage']['total_tokens'] == 319
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('changes', [
    {'confidence': 'low'}, {'description': None}, {'description': ''},
])
def test_unknown_or_low_confidence_records_abstain(monkeypatch, changes):
    assert deepseek.describe(ALBUM, lambda payload: response(**changes))['description'] is None


@pytest.mark.parametrize('bad', [
    {}, [], {'model': deepseek.MODEL, 'choices': []},
    response(description='Too short.'), response(description=23),
    response(confidence='certain'), response(uncertainty=['not prose']),
    response(description=TEXT + ' See https://example.com/.'),
    response(description=TEXT + ' Pitchfork gave it a perfect rating.'),
    response(description=TEXT + ' <script>alert(1)</script>'),
    {**response(), 'model': 'deepseek-v4-pro'},
    {**response(), 'choices': [None]},
    {**response(), 'choices': [{'finish_reason': 'stop', 'message': None}]},
    {**response(), 'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
])
def test_invalid_truncated_or_wrong_model_copy_is_rejected(monkeypatch, bad):
    with pytest.raises(deepseek.DescriptionUnavailable, match='invalid_response'):
        deepseek.describe(ALBUM, lambda payload: bad)


@pytest.mark.parametrize('status,reason', [(401, 'auth'), (403, 'auth'), (402, 'balance'),
                                         (429, 'rate_limit'), (503, 'unavailable')])
def test_failures_expose_only_safe_categories(monkeypatch, status, reason):
    def unavailable(*args):
        raise urllib.error.HTTPError(deepseek.ENDPOINT, status, 'SECRET', {}, io.BytesIO(b'SECRET'))
    iao = DeepSeekIAO(open_fn=lambda *args, **kwargs: unavailable())
    with pytest.raises(deepseek.DescriptionUnavailable) as caught:
        iao._request({'model': deepseek.MODEL}, 'SECRET')
    assert str(caught.value) == reason and caught.value.__suppress_context__


def test_transport_refuses_redirects_without_forwarding_credentials(monkeypatch):
    reached = []
    class Redirect(urllib.request.BaseHandler):
        handler_order = 100
        def https_open(self, request):
            reached.append(request.full_url)
            headers = Message()
            headers['Location'] = 'https://musicbrainz.org/should-not-receive-credentials'
            result = urllib.response.addinfourl(io.BytesIO(), headers, request.full_url, code=307)
            result.msg = 'Temporary Redirect'
            return result
    build_opener = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, 'build_opener',
        lambda *handlers: build_opener(urllib.request.ProxyHandler({}), Redirect(), *handlers))
    with pytest.raises(deepseek.DescriptionUnavailable, match='unavailable'):
        DeepSeekIAO()._request({'model': deepseek.MODEL}, 'secret')
    assert reached == [deepseek.ENDPOINT]
