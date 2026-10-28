"""Cover Art Archive metadata admission; urllib retains its guarded redirect policy."""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

API = 'https://coverartarchive.org'
USER_AGENT = 'yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )'
TIMEOUT_S = 20
MIN_INTERVAL = .25
CACHE_SIZE = 1024
COVER_TTL = 86400
MISS_TTL = 21600
ERROR_RETRY = 60
MAX_ERROR_RETRY = 900
_LOG = logging.getLogger(__name__)


class CoverArtArchiveIAO:
    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now, self.monotonic = now_fn, monotonic_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(MIN_INTERVAL)
        self._lock = threading.RLock()
        self._scope = ContextVar('coverartarchive_access_scope', default=None)
        self._availability = Availability()
        self._failures = 0
        self._cache, self._waits = OrderedDict(), OrderedDict()

    @contextmanager
    def context(self, store=None):
        token = self._scope.set(store) if store is not None else None
        try:
            yield self
        finally:
            if token is not None:
                self._scope.reset(token)

    def _state(self):
        return getattr(self._scope.get(), 'enrichment_state', None)

    def availability(self):
        with self._lock:
            state = self._state()
            saved = state.integration_cooldown('coverartarchive') if state else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict) or data.get('error') is not None
                or not isinstance(data.get('images'), list)):
            raise ValueError('Cover Art Archive response has no images list')
        for image in data['images']:
            if not isinstance(image, dict) or not isinstance(image.get('front'), bool):
                raise ValueError('Cover Art Archive image is malformed')
            thumbs = image.get('thumbnails', {})
            if not isinstance(thumbs, dict):
                raise ValueError('Cover Art Archive thumbnails are malformed')
            urls = [image.get('image'), *thumbs.values()]
            if any(url is not None and not isinstance(url, str) for url in urls):
                raise ValueError('Cover Art Archive image URL is malformed')
            if image['front'] and not any((thumbs.get('500'), thumbs.get('large'),
                                           thumbs.get('250'), image.get('image'))):
                raise ValueError('Cover Art Archive front image has no URL')

    def _error(self, wait, *, outage=True):
        error = IntegrationUnavailable('coverartarchive', wait.error, wait.retry_at, outage=outage)
        net.record_probe_failure(error)
        return error

    def release(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', identity):
            raise ValueError('Unsupported Cover Art Archive release')
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            cached = self._cache.get(identity)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(identity)
                return deepcopy(cached[0])
            admitted = self.availability()
            request_wait = self._waits.get(identity, (Availability(), False, 0))
            wait, outage, _ = max(((admitted, True, self._failures), request_wait),
                                 key=lambda value: value[0].retry_at)
            if wait.ready(self.now()):
                self.pacer.wait()
                check_cancellable_work()
                admitted = self.availability()
                if admitted.retry_at > wait.retry_at:
                    wait, outage = admitted, True
            if not wait.ready(self.now()):
                error = self._error(wait, outage=outage)
                if cached and cached[2]:
                    self._cache.move_to_end(identity)
                    return deepcopy(cached[0])
                raise error
            request = urllib.request.Request(f'{API}/release/{identity}',
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            started = self.monotonic()
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response:
                    data = json.load(response)
                self._validate(data)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    exc.close()
                    data = {'images': []}
                else:
                    error = self._failed(exc, identity, request_wait[2])
                    if isinstance(exc, urllib.error.HTTPError):
                        exc.close()
                    _LOG.info('Cover Art Archive outcome=retry retry_at=%.3f elapsed=%.3f',
                              error.retry_at, self.monotonic() - started)
                    if cached and cached[2]:
                        self._cache.move_to_end(identity)
                        return deepcopy(cached[0])
                    raise error from exc
            self._failures = 0
            self._availability = Availability()
            state = self._state()
            if state:
                state.clear_integration_cooldown('coverartarchive', admitted.retry_at)
            self._waits.pop(identity, None)
            useful = any(image['front'] for image in data['images'])
            self._cache[identity] = (deepcopy(data), self.monotonic() +
                                     (COVER_TTL if useful else MISS_TTL), useful)
            self._cache.move_to_end(identity)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return data

    def _failed(self, exc, identity, previous_failures):
        now = self.now()
        outage = net.provider_outage(exc)
        failures = min((self._failures if outage else previous_failures) + 1, 5)
        message = (f'Cover Art Archive HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError)
                   else f'Cover Art Archive {type(exc).__name__}')
        retry_at = max(now + min(ERROR_RETRY * 2 ** (failures - 1), MAX_ERROR_RETRY),
                       net.retry_after(exc, now))
        if outage:
            self._failures = failures
            state = self._state()
            if state:
                retry_at = state.defer_integration('coverartarchive', now=now,
                    retry_after=retry_at, error=message)['retry_at']
            self._availability = Availability(retry_at, message)
        wait = Availability(retry_at, message)
        self._waits[identity] = (wait, outage, failures)
        self._waits.move_to_end(identity)
        while len(self._waits) > CACHE_SIZE:
            self._waits.popitem(last=False)
        return self._error(wait, outage=outage)
