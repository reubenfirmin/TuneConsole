import io
import json
import urllib.error
import urllib.request

import pytest

from yt_playlist.providers import transport
from yt_playlist.util import net


@pytest.mark.parametrize('outcome', ['success', 'timeout', 'invalid_json', 'missing'])
def test_json_transport_preserves_pacing_timeout_and_breaker(outcome, monkeypatch):
    events, records = [], []
    breaker = net.CircuitBreaker()
    original_record = breaker.record

    def record(exc=None):
        records.append(exc)
        original_record(exc)

    monkeypatch.setattr(breaker, 'record', record)
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

    monkeypatch.setattr(transport.urllib.request, 'urlopen', open_request)

    def fetch():
        request = urllib.request.Request('https://example.test/track',
            headers={'User-Agent': 'transport-test', 'Accept': 'application/json'})
        return transport.request_json(request, timeout=8,
            pace=lambda *args: events.append(('pace', args)), breaker=breaker)

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
    assert timeout == 8
    assert request.get_header('User-agent') == 'transport-test'
    assert request.get_header('Accept') == 'application/json'
    assert len(records) == 1
    assert breaker.consecutive == (1 if outcome == 'timeout' else 0)
    assert len(errors) == (1 if outcome in ('timeout', 'invalid_json') else 0)
    assert all(response.closed for response in responses)
