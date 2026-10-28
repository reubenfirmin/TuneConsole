"""Last.fm access policy, shared by enrichment and recommendation callers.

API outages apply to every API method. Album pages have their own availability:
website failures must not stop the working metadata API. Both use one pacer.
Only short repository calls surround admission; no database lock spans HTTP.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.providers import credentials
from yt_playlist.util import net

API = 'https://ws.audioscrobbler.com/2.0/'
USER_AGENT = 'yt-playlist/0.1 ( https://4rc.io ; rf@4rc.io )'
TIMEOUT_S = 20
MIN_INTERVAL = 0.25
CACHE_SIZE = 512  # Per host: at most 1,024 responses and 1,024 request/credential waits in total.
POSITIVE_TTL = 86400
MISS_TTL = 3600
PAGE_FAIL_LIMIT = 5
PAGE_COOLDOWN_S = 900
_LOG = logging.getLogger(__name__)
_ENVELOPES = {'track.getinfo': 'track', 'artist.gettoptags': 'toptags',
              'tag.gettopartists': 'topartists', 'artist.getsimilar': 'similarartists'}


class _ApiError(ValueError):
    def __init__(self, code, headers=None):
        self.code = code
        self.headers = headers
        # Never retain a vendor message that might echo a credential or query.
        super().__init__(f'Last.fm API error {code}')


class LastFmIAO:
    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now = now_fn
        self.monotonic = monotonic_fn
        self.open = open_fn  # Resolve urllib at request time, including its installed egress guard.
        self.pacer = pacer or RateLimiter(MIN_INTERVAL)
        self._locks = {scope: threading.RLock() for scope in ('api', 'pages')}
        self._scope = ContextVar('lastfm_access_scope', default=None)
        self._availability = {'api': Availability(), 'pages': Availability()}
        self._failures = {'api': 0, 'pages': 0}
        self._waits = {scope: OrderedDict() for scope in self._locks}
        self._cache = {scope: OrderedDict() for scope in self._locks}

    @contextmanager
    def context(self, store=None):
        token = self._scope.set(store) if store is not None else None
        try:
            yield self
        finally:
            if token is not None:
                self._scope.reset(token)

    def api_key(self, store=None, config_path=None):
        return credentials.resolve(env_name='LASTFM_API_KEY', setting_name='lastfm_api_key',
                                   store=store if store is not None else self._scope.get(),
                                   config_path=config_path)

    def _state(self):
        return getattr(self._scope.get(), 'enrichment_state', None)

    @staticmethod
    def _provider(scope):
        return 'lastfm' if scope == 'api' else 'lastfm:pages'

    def availability(self, scope='api'):
        with self._locks[scope]:
            state = self._state()
            saved = state.integration_cooldown(self._provider(scope)) if state else None
            current = self._availability[scope]
            if saved and saved['retry_at'] > current.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return current

    @staticmethod
    def _miss(data, params):
        # Code 6 also means invalid parameters. Only a resource-not-found answer
        # to a complete supported query establishes absence.
        method = str(params.get('method', '')).lower()
        required = {'track.getinfo': ('artist', 'track'), 'artist.gettoptags': ('artist',),
                    'artist.getsimilar': ('artist',), 'tag.gettopartists': ('tag',)}.get(method)
        return (str(data.get('error')) == '6' and required
                and all(str(params.get(field) or '').strip() for field in required)
                and bool(re.search(r'\b(?:not found|could not find)\b',
                                   str(data.get('message', '')), re.I)))

    @staticmethod
    def _useful(data, params):
        if data.get('error'):
            return False
        method = str(params['method']).lower()
        content = data[_ENVELOPES[method]]
        if method == 'track.getinfo':
            return bool(content)
        return bool(content.get('tag' if method == 'artist.gettoptags' else 'artist'))

    @staticmethod
    def _validate(data, method):
        content = data.get(_ENVELOPES[method])
        if not isinstance(content, dict):
            raise ValueError('Last.fm response has no expected result')

        def names(payload, field):
            rows = payload.get(field) or []
            rows = [rows] if isinstance(rows, dict) else rows
            if not isinstance(rows, list) or any(not isinstance(row, dict)
                    or row.get('name') is not None and not isinstance(row['name'], str) for row in rows):
                raise ValueError('Last.fm result contains invalid names')

        if method == 'track.getinfo':
            for field in ('name', 'mbid', 'url'):
                if content.get(field) is not None and not isinstance(content[field], str):
                    raise ValueError('Last.fm track contains invalid identifiers')
            credit = content.get('artist')
            if credit is not None and not isinstance(credit, (dict, str)):
                raise ValueError('Last.fm track contains an invalid artist')
            if isinstance(credit, dict) and credit.get('name') is not None and not isinstance(credit['name'], str):
                raise ValueError('Last.fm track contains an invalid artist name')
            for field in ('album', 'toptags'):
                if content.get(field) is not None and not isinstance(content[field], dict):
                    raise ValueError('Last.fm track contains invalid metadata')
            url = (content.get('album') or {}).get('url')
            if url is not None and not isinstance(url, str):
                raise ValueError('Last.fm album contains an invalid URL')
            names(content.get('toptags') or {}, 'tag')
        else:
            names(content, 'tag' if method == 'artist.gettoptags' else 'artist')

    def get(self, params, *, timeout=TIMEOUT_S):
        check_cancellable_work()
        with self._locks['api']:
            return self._get(params, timeout=timeout)

    def _get(self, params, *, timeout):
        params = {**params}
        explicit_key = params.pop('api_key', None)
        # A worker may have resolved its key before the user edited Setup. In an
        # application scope, resolve the current credential at every admission.
        store = self._scope.get()
        key = self.api_key() if hasattr(store, 'settings') else explicit_key or self.api_key()
        method = str(params.get('method', '')).lower()
        if not key or method not in _ENVELOPES:
            error = IntegrationUnavailable('lastfm', 'Last.fm API key or method is missing',
                                           self.now() + 30, outage=False)
            net.record_probe_failure(error)
            raise error
        params['method'] = method
        params.setdefault('format', 'json')
        credential = sha256(key.encode()).digest()
        # Cache identity contains a digest, never the API key itself.
        cache_key = ('api', credential, tuple(sorted((k, str(v)) for k, v in params.items())))
        params['api_key'] = key
        request = urllib.request.Request(API + '?' + urllib.parse.urlencode(params),
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})

        def decode(response):
            data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError('Last.fm response is not an object')
            if data.get('error'):
                if not self._miss(data, params):
                    raise _ApiError(int(data['error']), getattr(response, 'headers', None))
            else:
                self._validate(data, method)
            return data

        return self._request('api', cache_key, request, decode,
                             lambda data: self._useful(data, params), timeout,
                             credential=credential, endpoint=method, params=params)

    def page(self, url, *, timeout=TIMEOUT_S):
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        if (parts.scheme not in ('http', 'https') or parts.username or parts.password
                or not (host == 'last.fm' or host.endswith('.last.fm'))):
            raise ValueError('Album URL is not a Last.fm page')
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        return self._request('pages', ('pages', url), request,
                             lambda response: response.read().decode('utf-8', 'replace'),
                             bool, timeout, endpoint='album-page')

    def _error(self, scope, availability, *, outage=True):
        error = IntegrationUnavailable(self._provider(scope), availability.error,
                                       availability.retry_at, outage=outage)
        net.record_probe_failure(error)
        return error

    def _request(self, scope, key, request, decode, useful, timeout, *,
                 credential=None, endpoint, params=None):
        started = self.monotonic()
        check_cancellable_work()
        with self._locks[scope]:
            check_cancellable_work()
            cache, waits = self._cache[scope], self._waits[scope]
            cached = cache.get(key)
            if cached and self.monotonic() < cached[1]:
                cache.move_to_end(key)
                _LOG.debug('Last.fm request endpoint=%s outcome=cache', endpoint)
                return deepcopy(cached[0])
            availability = self.availability(scope)
            candidates = [(availability, True), waits.get(key, (Availability(), False)),
                          waits.get(('credential', credential), (Availability(), False))
                          if credential else (Availability(), False)]
            wait, outage = max(candidates, key=lambda item: item[0].retry_at)
            if wait.ready(self.now()):
                self.pacer.wait()
                check_cancellable_work()
                availability = self.availability(scope)
                if availability.retry_at > wait.retry_at:
                    wait, outage = availability, True
            if not wait.ready(self.now()):
                error = self._error(scope, wait, outage=outage)
                _LOG.debug('Last.fm request endpoint=%s outcome=deferred retry_at=%.3f',
                           endpoint, error.retry_at)
                if cached and cached[2]:
                    cache.move_to_end(key)
                    return deepcopy(cached[0])
                raise error
            if credential and hasattr(self._scope.get(), 'settings'):
                current = self.api_key()
                if not current or sha256(current.encode()).digest() != credential:
                    # Setup can change while pacing; the queued caller's old key
                    # must not be sent or decide the new key's cache/admission.
                    return self.get(params, timeout=timeout)
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=timeout) as response:
                    data = decode(response)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if scope == 'pages' and isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    data = ''  # A missing album page is a confirmed miss, not an outage.
                else:
                    error = self._failed(scope, key, credential, exc)
                    _LOG.info('Last.fm request endpoint=%s outcome=retry retry_at=%.3f elapsed=%.3f',
                              endpoint, error.retry_at, self.monotonic() - started)
                    if cached and cached[2]:
                        cache.move_to_end(key)
                        return deepcopy(cached[0])
                    raise error from exc
            self._failures[scope] = 0
            self._availability[scope] = Availability()
            state = self._state()
            if state:
                state.clear_integration_cooldown(self._provider(scope), availability.retry_at)
            waits.pop(key, None)
            if credential:
                waits.pop(('credential', credential), None)
            positive = useful(data)
            cache[key] = (deepcopy(data), self.monotonic() +
                          (POSITIVE_TTL if positive else MISS_TTL), positive)
            cache.move_to_end(key)
            while len(cache) > CACHE_SIZE:
                cache.popitem(last=False)
            _LOG.debug('Last.fm request endpoint=%s outcome=success elapsed=%.3f',
                       endpoint, self.monotonic() - started)
            return data

    def _failed(self, scope, key, credential, exc):
        now = self.now()
        code = str(exc.code) if isinstance(exc, _ApiError) else None
        auth = (code in ('4', '9', '10', '13', '14', '26') or scope == 'api'
                and isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403))
        outage = net.provider_outage(exc) or code in ('8', '11', '16', '29')
        message = (str(exc) if isinstance(exc, _ApiError) else
                   f'Last.fm HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else
                   f'Last.fm {type(exc).__name__}')
        retry_at = max(now + 30, net.retry_after(exc, now))
        shared = outage and scope == 'api'
        if outage:
            self._failures[scope] += 1
            if scope == 'api':
                retry_at = max(retry_at, now + min(3600, 30 * 2 ** min(self._failures[scope] - 1, 7)))
            elif self._failures[scope] >= PAGE_FAIL_LIMIT:
                retry_at = max(retry_at, now + PAGE_COOLDOWN_S)
                shared = True
            # Honor an explicit page Retry-After without waiting for five failures.
            shared = shared or scope == 'pages' and (net.retry_after(exc, now) > now
                or isinstance(exc, urllib.error.HTTPError) and exc.code == 429)
        wait = Availability(retry_at, message)
        if shared:
            state = self._state()
            if state:
                saved = state.defer_integration(self._provider(scope), now=now,
                                               retry_after=retry_at, error=message)
                wait = Availability(saved['retry_at'], message)
            self._availability[scope] = wait
        wait_key = ('credential', credential) if auth else key
        waits = self._waits[scope]
        waits[wait_key] = (wait, outage)
        waits.move_to_end(wait_key)
        while len(waits) > CACHE_SIZE:
            waits.popitem(last=False)
        return self._error(scope, wait, outage=outage)
