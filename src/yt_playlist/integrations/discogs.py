"""Discogs access admission for authenticated searches and public release facts."""
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

API = 'https://api.discogs.com'
USER_AGENT = 'yt-playlist/0.1 +https://4rc.io'
TIMEOUT_S = 20
CACHE_SIZE = 512
POSITIVE_TTL = 86400
MISS_TTL = 3600
_LOG = logging.getLogger(__name__)


class DiscogsIAO:
    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now = now_fn
        self.monotonic = monotonic_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(2.5)
        self._lock = threading.RLock()
        self._scope = ContextVar('discogs_access_scope', default=None)
        self._availability = Availability()
        self._failures = 0
        self._cache = OrderedDict()
        self._waits = OrderedDict()

    @contextmanager
    def context(self, store=None):
        token = self._scope.set(store) if store is not None else None
        try:
            yield self
        finally:
            if token is not None:
                self._scope.reset(token)

    def token(self, store=None, config_path=None):
        return credentials.resolve(env_name='DISCOGS_TOKEN', setting_name='discogs_token',
            store=store if store is not None else self._scope.get(), config_path=config_path)

    def _state(self):
        return getattr(self._scope.get(), 'enrichment_state', None)

    def availability(self):
        with self._lock:
            state = self._state()
            saved = state.integration_cooldown('discogs') if state else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    def search(self, params, *, tok=None, timeout=TIMEOUT_S):
        return self.get('database/search', params, tok, timeout=timeout)

    def release(self, identity, *, tok=None, timeout=TIMEOUT_S):
        return self.get(f'releases/{identity}', {}, tok, timeout=timeout)

    def master(self, identity, *, tok=None, timeout=TIMEOUT_S):
        return self.get(f'masters/{identity}', {}, tok, timeout=timeout)

    @staticmethod
    def _validate(path, data):
        def text(row, field, *, required=False):
            value = row.get(field)
            if (required and not value) or (value is not None and not isinstance(value, str)):
                raise ValueError(f'Discogs has an invalid {field}')

        def rows(row, field):
            values = row.get(field)
            if values is None:
                return []
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise ValueError(f'Discogs has invalid {field}')
            return values

        if not isinstance(data, dict):
            raise ValueError('Discogs response is not an object')
        if path == 'database/search':
            results = data.get('results')
            if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
                raise ValueError('Discogs search has no valid results')
            for row in results:
                text(row, 'title', required=True)
                if not re.fullmatch(r'[1-9]\d*', str(row.get('id', ''))):
                    raise ValueError('Discogs search has an invalid identity')
                for field in ('genre', 'style'):
                    values = row.get(field) if row.get(field) is not None else []
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                        raise ValueError('Discogs search has invalid tags')
            return
        if str(data.get('id')) != path.split('/')[1]:
            raise ValueError('Discogs response has a different or missing identity')
        text(data, 'title', required=True)
        text(data, 'notes')
        text(data, 'uri')
        if not rows(data, 'artists'):
            raise ValueError('Discogs response has no artists')
        for field in ('artists', 'labels'):
            for row in rows(data, field):
                text(row, 'name', required=True)
        community = data.get('community')
        if community is not None:
            if not isinstance(community, dict):
                raise ValueError('Discogs has invalid community ratings')
            if community.get('rating') is not None and not isinstance(community['rating'], dict):
                raise ValueError('Discogs has invalid community ratings')
        if data.get('main_release') is not None and not re.fullmatch(r'[1-9]\d*', str(data['main_release'])):
            raise ValueError('Discogs has an invalid main release')
        pending = [(data, 0)]
        while pending:
            row, depth = pending.pop()
            if depth > 32:
                raise ValueError('Discogs track nesting is too deep')
            text(row, 'title')
            text(row, 'type_')
            for artist in rows(row, 'artists'):
                text(artist, 'name', required=True)
            pending.extend((track, depth + 1) for track in rows(row, 'tracklist') + rows(row, 'sub_tracks'))

    def _error(self, wait, *, outage=True):
        error = IntegrationUnavailable('discogs', wait.error, wait.retry_at, outage=outage)
        net.record_probe_failure(error)
        return error

    def get(self, path, params, tok=None, *, timeout=TIMEOUT_S):
        if path != 'database/search' and not re.fullmatch(r'(?:masters|releases)/[1-9]\d*', path):
            raise ValueError('Unsupported Discogs resource')
        params = {key: value for key, value in params.items() if key != 'token'}
        search = path == 'database/search'
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            scoped = hasattr(self._scope.get(), 'settings')
            configured = self.token() if scoped else tok or self.token()
            digest = sha256(configured.encode()).digest() if configured else None
            auth_wait = self._waits.get(('credential', digest), (Availability(), False))[0]
            # A known bad optional token must not prevent unrelated public reads.
            used_token = configured if search or auth_wait.ready(self.now()) else None
            if search and not configured:
                raise self._error(Availability(self.now() + 30,
                    'Discogs search requires a token'), outage=False)
            # Public responses have user_data removed before sharing. Search cache
            # identity includes the credential digest; no token enters cache keys.
            key = (path, tuple(sorted((name, str(value)) for name, value in params.items())),
                   digest if search else None)
            cached = self._cache.get(key)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(key)
                _LOG.debug('Discogs request endpoint=%s outcome=cache', path.split('/')[0])
                return deepcopy(cached[0])
            availability = self.availability()
            candidates = [(availability, True), self._waits.get(key, (Availability(), False))]
            if search:
                candidates.append((auth_wait, False))
            wait, outage = max(candidates, key=lambda value: value[0].retry_at)
            if wait.ready(self.now()):
                self.pacer.wait(1.1 if used_token else 2.5)
                check_cancellable_work()
                availability = self.availability()
                if availability.retry_at > wait.retry_at:
                    wait, outage = availability, True
            if not wait.ready(self.now()):
                error = self._error(wait, outage=outage)
                _LOG.debug('Discogs request endpoint=%s outcome=deferred retry_at=%.3f',
                           path.split('/')[0], error.retry_at)
                if cached and cached[2]:
                    self._cache.move_to_end(key)
                    return deepcopy(cached[0])
                raise error
            if scoped and self.token() != configured:
                return self.get(path, params, timeout=timeout)
            request_params = {**params, **({'token': used_token} if used_token else {})}
            request = urllib.request.Request(API + '/' + path +
                ('?' + urllib.parse.urlencode(request_params) if request_params else ''),
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            started = self.monotonic()
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=timeout) as response:
                    data = json.load(response)
                self._validate(path, data)
                if not search:
                    data.pop('user_data', None)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if not search and isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    data = {}
                else:
                    error = self._failed(exc, key, digest if used_token else None)
                    _LOG.info('Discogs request endpoint=%s outcome=retry retry_at=%.3f elapsed=%.3f',
                              path.split('/')[0], error.retry_at, self.monotonic() - started)
                    if cached and cached[2]:
                        self._cache.move_to_end(key)
                        return deepcopy(cached[0])
                    raise error from exc
            self._failures = 0
            self._availability = Availability()
            state = self._state()
            if state:
                state.clear_integration_cooldown('discogs', availability.retry_at)
            self._waits.pop(key, None)
            if used_token:
                self._waits.pop(('credential', digest), None)
            useful = bool(data.get('results')) if search else bool(data)
            self._cache[key] = (deepcopy(data), self.monotonic() +
                                (POSITIVE_TTL if useful else MISS_TTL), useful)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            _LOG.debug('Discogs request endpoint=%s outcome=success elapsed=%.3f',
                       path.split('/')[0], self.monotonic() - started)
            return data

    def _failed(self, exc, key, credential):
        now = self.now()
        outage = net.provider_outage(exc)
        auth = credential is not None and isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)
        message = f'Discogs HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else f'Discogs {type(exc).__name__}'
        retry_at = max(now + 30, net.retry_after(exc, now))
        if outage:
            self._failures += 1
            retry_at = max(retry_at, now + min(3600, 30 * 2 ** min(self._failures - 1, 7)))
            state = self._state()
            if state:
                retry_at = state.defer_integration('discogs', now=now,
                    retry_after=retry_at, error=message)['retry_at']
            self._availability = Availability(retry_at, message)
        wait = Availability(retry_at, message)
        wait_key = ('credential', credential) if auth else key
        self._waits[wait_key] = (wait, outage)
        self._waits.move_to_end(wait_key)
        while len(self._waits) > CACHE_SIZE:
            self._waits.popitem(last=False)
        return self._error(wait, outage=outage)
