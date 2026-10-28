"""Deezer public catalog access, shared by enrichment and candidate acquisition."""
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

API = 'https://api.deezer.com'
USER_AGENT = 'yt-playlist/0.1 +https://4rc.io'
TIMEOUT_S = 20
MIN_INTERVAL = .15
CACHE_SIZE = 512
POSITIVE_TTL = 86400
MISS_TTL = 3600
_LOG = logging.getLogger(__name__)


class _ApiError(ValueError):
    def __init__(self, code, headers):
        self.code, self.headers = code, headers
        # Do not retain a vendor message that can echo the search query.
        super().__init__(f'Deezer API error {code}')


class DeezerIAO:
    """Own transport, admission, response sharing and recoverable failure policy."""

    configured = True  # Public catalog reads require no credentials.

    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now, self.monotonic = now_fn, monotonic_fn
        self.open = open_fn  # Resolve the installed egress guard at request time.
        self.pacer = pacer or RateLimiter(MIN_INTERVAL)
        self._lock = threading.RLock()
        self._scope = ContextVar('deezer_access_scope', default=None)
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
            saved = state.integration_cooldown('deezer') if state is not None else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    @staticmethod
    def _request_key(url):
        parts = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if (parts.scheme != 'https' or parts.netloc != 'api.deezer.com' or parts.fragment
                or len({name for name, _value in params}) != len(params)
                or not (parts.path == '/search/track'
                        or any(parts.path.startswith('/' + kind + '/')
                               and parts.path[len(kind) + 2:].isdigit() for kind in ('track', 'album')))):
            raise ValueError('Unsupported Deezer catalog endpoint')
        return parts.path, tuple(sorted(params))

    @staticmethod
    def _validate(path, data):
        def text(value):
            return value is None or isinstance(value, str)

        def credit(value):
            return isinstance(value, dict) and text(value.get('name'))

        def identity(value):
            return type(value) is int and value > 0

        def track(value):
            contributors = value.get('contributors') if isinstance(value, dict) else None
            return (isinstance(value, dict) and identity(value.get('id'))
                    and isinstance(value.get('title'), str) and text(value.get('title_version'))
                    and (value.get('artist') is None or credit(value['artist']))
                    and (contributors is None or isinstance(contributors, list)
                         and all(credit(c) for c in contributors))
                    and (value.get('album') is None or isinstance(value['album'], dict)))

        if path == '/search/track':
            if not isinstance(data.get('data'), list) or not all(track(row) for row in data['data']):
                raise ValueError('Deezer search has no valid result list')
        elif path.startswith('/track/'):
            if (not track(data) or data['id'] != int(path.rsplit('/', 1)[1])
                    or not isinstance(data.get('artist'), dict)
                    or not isinstance(data['artist'].get('name'), str)):
                raise ValueError('Deezer track has no valid identity')
            if not text(data.get('release_date')) or not text(data.get('isrc')):
                raise ValueError('Deezer track has invalid recording metadata')
            for field in ('bpm', 'gain', 'rank', 'duration'):
                value = data.get(field)
                if value is not None and (not isinstance(value, (int, float))
                                          or isinstance(value, bool) or not math.isfinite(value)):
                    raise ValueError('Deezer track has invalid numeric metadata')
            album = data.get('album') or {}
            if (album.get('id') is not None and not identity(album['id'])
                    or any(not text(album.get(field)) for field in ('cover_xl', 'cover_big'))):
                raise ValueError('Deezer track has invalid album metadata')
        else:
            genres = (data.get('genres') or {}).get('data', []) if isinstance(data.get('genres'), dict) else []
            if (not identity(data.get('id')) or data['id'] != int(path.rsplit('/', 1)[1])
                    or not text(data.get('label'))
                    or not text(data.get('release_date'))
                    or data.get('genres') is not None and not isinstance(data['genres'], dict)
                    or not isinstance(genres, list) or not all(credit(genre) for genre in genres)):
                raise ValueError('Deezer album has invalid metadata')

    @classmethod
    def _decode(cls, path, response):
        data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError('Deezer response is not an object')
        if 'error' in data:
            error = data['error']
            if not isinstance(error, dict):
                raise ValueError('Deezer error is not an object')
            raw = error.get('code')
            code = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
            if code != 800:
                raise _ApiError(code, getattr(response, 'headers', None))
            return {'error': {'code': 800}}  # Confirmed absence, without retaining vendor text.
        cls._validate(path, data)
        return data

    @staticmethod
    def _bounded_put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > CACHE_SIZE:
            cache.popitem(last=False)

    @staticmethod
    def _error(wait, *, outage):
        error = IntegrationUnavailable('deezer', wait.error or 'Deezer is backing off',
                                       wait.retry_at, outage=outage)
        net.record_probe_failure(error)
        return error

    def _failed(self, key, exc):
        now = self.now()
        outage = net.provider_outage(exc) or isinstance(exc, _ApiError) and exc.code in (4, 700)
        message = (str(exc) if isinstance(exc, _ApiError) else
                   f'Deezer HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError)
                   else 'Deezer ' + type(exc).__name__)
        retry_at = max(now + 30, net.retry_after(exc, now))
        if outage:
            self._failures += 1
            retry_at = max(retry_at, now + min(3600, 30 * 2 ** min(self._failures - 1, 7)))
            state = self._scope.get()
            if state is not None:
                saved = state.defer_integration('deezer', now=now, retry_after=retry_at, error=message)
                retry_at = saved['retry_at']
            self._availability = Availability(retry_at, message)
        else:
            self._bounded_put(self._waits, key, Availability(retry_at, message))
        return self._error(Availability(retry_at, message), outage=outage)

    def get(self, url, *, timeout=TIMEOUT_S):
        key = self._request_key(url)
        endpoint = key[0].split('/')[1]
        started = self.monotonic()
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            cached = self._cache.get(key)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(key)
                return deepcopy(cached[0])
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
                error = self._error(wait, outage=outage)
                if cached and cached[2]:
                    self._cache.move_to_end(key)
                    return deepcopy(cached[0])
                raise error
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=timeout) as response:
                    data = self._decode(key[0], response)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404 and key[0] != '/search/track':
                    data = {'error': {'code': 800}}
                else:
                    error = self._failed(key, exc)
                    _LOG.info('Deezer request endpoint=%s outcome=retry retry_at=%.3f elapsed=%.3f',
                              endpoint, error.retry_at, self.monotonic() - started)
                    if cached and cached[2]:
                        self._cache.move_to_end(key)
                        return deepcopy(cached[0])
                    raise error from exc
            self._failures = 0
            self._availability = Availability()
            state = self._scope.get()
            if state is not None:
                state.clear_integration_cooldown('deezer', availability.retry_at)
            self._waits.pop(key, None)
            useful = not data.get('error') and (bool(data['data']) if key[0] == '/search/track' else True)
            self._bounded_put(self._cache, key, (deepcopy(data), self.monotonic() +
                              (POSITIVE_TTL if useful else MISS_TTL), useful))
            _LOG.debug('Deezer request endpoint=%s outcome=success elapsed=%.3f',
                       endpoint, self.monotonic() - started)
            return data
