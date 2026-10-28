"""HTTPError owns a response even when urlopen never enters its with block."""
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from yt_playlist.integrations import discogs, lastfm
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.util import net


@pytest.mark.parametrize('resource', [
    'lastfm-api', 'lastfm-page', 'discogs-search', 'discogs-release', 'discogs-master',
])
@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('status', [404, 401, 429, 503])
def test_error_body_is_closed_before_miss_retry_or_retained_return(resource, cached, status):
    clock, failure, calls, responses = [1000.], [None], [], []
    if resource == 'lastfm-page':
        good = '<html>Album</html>'
    elif resource == 'lastfm-api':
        good = {'toptags': {'tag': [{'name': 'House'}]}}
    elif resource == 'discogs-search':
        good = {'results': [{'id': 123, 'title': 'Artist - Album'}]}
    else:
        good = {'id': 123, 'title': 'Album', 'artists': [{'name': 'Artist'}]}

    def open_request(request, **kwargs):
        calls.append(request)
        if failure[0] is not None:
            raise failure[0]
        response = io.BytesIO((good if isinstance(good, str) else json.dumps(good)).encode())
        responses.append(response)
        return response

    options = dict(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0], open_fn=open_request,
                   pacer=SimpleNamespace(wait=lambda *args: None))
    if resource.startswith('lastfm'):
        iao, ttl = lastfm.LastFmIAO(**options), lastfm.POSITIVE_TTL
        read = (lambda: iao.page('https://www.last.fm/music/Artist/Album')) if resource == 'lastfm-page' else (
            lambda: iao.get({'method': 'artist.gettoptags', 'artist': 'Artist', 'api_key': 'secret'}))
    else:
        iao, ttl = discogs.DiscogsIAO(**options), discogs.POSITIVE_TTL
        if resource == 'discogs-search':
            read = lambda: iao.search({'q': 'Artist Album'}, tok='secret')
        elif resource == 'discogs-release':
            read = lambda: iao.release(123, tok='secret')
        else:
            read = lambda: iao.master(123, tok='secret')
    if cached:
        assert read() == good
        clock[0] += ttl
    body = io.BytesIO(b'controlled HTTP failure body')
    failure[0] = urllib.error.HTTPError('https://controlled.invalid/', status,
                                       'controlled error', {'Retry-After': '120'}, body)
    miss = status == 404 and resource in ('lastfm-page', 'discogs-release', 'discogs-master')
    with net.probe_errors() as errors:
        if miss:
            assert read() == ('' if resource == 'lastfm-page' else {})
        elif cached:
            assert read() == good
        else:
            with pytest.raises(IntegrationUnavailable):
                read()
    assert body.closed and all(response.closed for response in responses)
    assert len(calls) == 1 + int(cached)
    if miss:
        assert not errors
    else:
        assert len(errors) == 1 and errors[0].retry_at == clock[0] + 120
        assert errors[0].outage == (status in (429, 503))
        if status == 401:
            # A rejected credential must not become a host-wide outage.
            scope = ('pages',) if resource == 'lastfm-page' else ('api',)
            availability = iao.availability(*scope) if resource.startswith('lastfm') else iao.availability()
            assert availability.ready(clock[0])
