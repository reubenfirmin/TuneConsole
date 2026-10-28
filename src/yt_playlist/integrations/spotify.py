"""Public Spotify oEmbed access for optional import-report artwork."""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

OEMBED_URL = 'https://open.spotify.com/oembed'
USER_AGENT = 'TuneConsole/0.1 (https://tuneconsole.com)'
TIMEOUT_S = 8
MIN_INTERVAL = .2  # Local admission policy, not a claim about Spotify's quota.
CACHE_SIZE = 512
POSITIVE_TTL = 86400
MISS_TTL = 3600
RETRY_S = 60
MAX_RESPONSE_BYTES = 256 * 1024
_LOG = logging.getLogger(__name__)


def canonical_album_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    path = parts.path.rstrip('/').split('/')
    if (parts.scheme != 'https' or parts.netloc.lower() != 'open.spotify.com'
            or len(path) != 3 or path[1] != 'album'
            or not path[2].isascii() or not path[2].isalnum()):
        return None
    return f'https://open.spotify.com/album/{path[2]}'


class SpotifyIAO:
    """Own public transport, response sharing, admission and recoverable failure."""

    configured = True

    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic, open_fn=None, pacer=None):
        self.now, self.monotonic = now_fn, monotonic_fn
        self.open = open_fn  # Resolve the installed egress guard at request time.
        self.pacer = pacer or RateLimiter(MIN_INTERVAL)
        self._lock = threading.RLock()
        self._scope = ContextVar('spotify_access_scope', default=None)
        self._availability = Availability()
        self._failures = 0
        self._cache, self._waits = OrderedDict(), OrderedDict()

    @contextmanager
    def context(self, state=None):
        token = self._scope.set(state) if state is not None else None
        try:
            yield self
        finally:
            if token is not None:
                self._scope.reset(token)

    def availability(self):
        with self._lock:
            state = self._scope.get()
            saved = state.integration_cooldown('spotify') if state is not None else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    def clear_cache(self):
        """Discard response data without evading vendor availability."""
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > CACHE_SIZE:
            cache.popitem(last=False)

    @staticmethod
    def _decode(response):
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('Spotify oEmbed response exceeds the byte limit')
        data = json.loads(raw)
        if (not isinstance(data, dict) or 'error' in data
                or data.get('type') != 'rich' or data.get('version') != '1.0'
                or data.get('provider_name') != 'Spotify'
                or not isinstance(data.get('html'), str) or not isinstance(data.get('title'), str)):
            raise ValueError('Spotify oEmbed response is invalid')
        url = data.get('thumbnail_url')
        if url is None:
            return None  # A valid oEmbed can explicitly have no thumbnail.
        if not isinstance(url, str) or not url or len(url) > 2048:
            raise ValueError('Spotify thumbnail URL is invalid')
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ''
        if (parts.scheme != 'https' or parts.netloc.lower() != host
                or not any(host == domain or host.endswith('.' + domain)
                           for domain in ('scdn.co', 'spotifycdn.com'))):
            raise ValueError('Spotify thumbnail URL is not a public Spotify image')
        return url

    def _unavailable(self, key, cached, wait, *, outage):
        error = IntegrationUnavailable('spotify', wait.error or 'Spotify access is backing off',
                                       wait.retry_at, outage=outage)
        net.record_probe_failure(error)
        if cached and cached[0] is not None:
            self._cache.move_to_end(key)
            return cached[0]
        raise error

    def _failed(self, key, exc):
        now = self.now()
        outage = net.provider_outage(exc)
        message = (f'Spotify oEmbed HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError)
                   else 'Spotify oEmbed ' + type(exc).__name__)
        retry_at = max(now + RETRY_S, net.retry_after(exc, now))
        if outage:
            self._failures += 1
            retry_at = max(retry_at, now + min(3600, RETRY_S * 2 ** min(self._failures - 1, 6)))
            state = self._scope.get()
            if state is not None:
                retry_at = state.defer_integration('spotify', now=now,
                    retry_after=retry_at, error=message)['retry_at']
            self._availability = Availability(retry_at, message)
        else:
            self._put(self._waits, key, Availability(retry_at, message))
        _LOG.info('Spotify oEmbed outcome=retry retry_at=%.3f', retry_at)
        return Availability(retry_at, message), outage

    def thumbnail(self, album_url):
        key = canonical_album_url(album_url)
        if key is None:
            return None
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            cached = self._cache.get(key)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(key)
                return cached[0]
            availability = self.availability()
            wait, outage = max(((availability, True), (self._waits.get(key, Availability()), False)),
                               key=lambda item: item[0].retry_at)
            if wait.ready(self.now()):
                self.pacer.wait()
                check_cancellable_work()
                availability = self.availability()
                if availability.retry_at > wait.retry_at:
                    wait, outage = availability, True
            if not wait.ready(self.now()):
                return self._unavailable(key, cached, wait, outage=outage)
            request = urllib.request.Request(OEMBED_URL + '?' + urllib.parse.urlencode({'url': key}),
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}, method='GET')
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response:
                    thumbnail = self._decode(response)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (404, 410):
                    thumbnail = None
                else:
                    wait, outage = self._failed(key, exc)
                    return self._unavailable(key, cached, wait, outage=outage)
            self._failures = 0
            self._availability = Availability()
            state = self._scope.get()
            if state is not None:
                state.clear_integration_cooldown('spotify', availability.retry_at)
            self._waits.pop(key, None)
            self._put(self._cache, key, (thumbnail, self.monotonic() +
                      (POSITIVE_TTL if thumbnail else MISS_TTL)))
            return thumbnail
