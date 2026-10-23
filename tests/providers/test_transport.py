import io
import json
import urllib.error
import urllib.parse

import pytest

from yt_playlist.providers import acousticbrainz, deezer, discogs, lastfm, musicbrainz, wikipedia
from yt_playlist.util import net


@pytest.mark.parametrize('provider', [musicbrainz, lastfm, discogs, deezer, acousticbrainz, wikipedia])
@pytest.mark.parametrize('outcome', ['success', 'timeout', 'invalid_json', 'missing'])
def test_adapter_json_requests_preserve_pacing_timeout_and_breaker(provider, outcome, monkeypatch):
    events, records = [], []
    breaker = net.CircuitBreaker()
    original_record = breaker.record

    def record(exc=None):
        records.append(exc)
        original_record(exc)

    monkeypatch.setattr(breaker, 'record', record)
    monkeypatch.setattr(provider, '_breaker', breaker)
    monkeypatch.setattr(provider._pacer, 'wait', lambda *args: events.append(('pace', args)))
    error = (TimeoutError('stalled') if outcome == 'timeout' else
             urllib.error.HTTPError('https://example.test', 404, 'missing', {}, None))
    responses = []

    def open_request(request, *, timeout):
        events.append(('request', request, timeout))
        if outcome in ('timeout', 'missing'):
            raise error
        response = io.StringIO('{"ok":true}' if outcome == 'success' else 'invalid json')
        responses.append(response)
        return response

    monkeypatch.setattr(provider.urllib.request, 'urlopen', open_request)

    def fetch():
        if provider is musicbrainz:
            return provider._get('recording', {'query': 'Song'})
        if provider is lastfm:
            return provider._get({'method': 'track.getinfo', 'api_key': 'key'})
        if provider is discogs:
            return provider._get('database/search', {'q': 'Song'}, 'token')
        return provider._get_json('https://example.test/track')

    with net.probe_errors() as errors:
        if outcome == 'success':
            assert fetch() == {'ok': True}
        else:
            expected = json.JSONDecodeError if outcome == 'invalid_json' else type(error)
            with pytest.raises(expected) as caught:
                fetch()
            if outcome != 'invalid_json':
                assert caught.value is error
    assert [event[0] for event in events] == ['pace', 'request']
    request, timeout = events[1][1:]
    assert timeout == provider._HTTP_TIMEOUT_S > 0
    assert request.get_header('User-agent') == provider._USER_AGENT
    assert request.get_header('Accept') == 'application/json'
    assert len(records) == 1
    assert breaker.consecutive == (1 if outcome == 'timeout' else 0)
    assert len(errors) == (1 if outcome in ('timeout', 'invalid_json') else 0)
    assert all(response.closed for response in responses)
    if provider is discogs:
        assert events[0][1] == (1.1,)
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)['token'] == ['token']


@pytest.mark.parametrize('provider,fetch,payload,missing', [
    (lastfm, lambda: lastfm._get({}), {'error': 29}, {'error': 6}),
    (deezer, lambda: deezer._get_json('https://example.test'), {'error': {'code': 4}}, {'error': {'code': 800}}),
])
def test_http_200_api_errors_keep_adapter_policy(provider, fetch, payload, missing, monkeypatch):
    monkeypatch.setattr(provider._pacer, 'wait', lambda: None)
    monkeypatch.setattr(provider, '_breaker', net.CircuitBreaker())
    for body, expected_errors in ((payload, 1), (missing, 0)):
        monkeypatch.setattr(provider.urllib.request, 'urlopen',
                            lambda *args, **kwargs: io.StringIO(json.dumps(body)))
        with net.probe_errors() as errors:
            assert fetch() == body
        assert len(errors) == expected_errors
