"""Shared admission and raw evidence for AcousticBrainz's two descriptor endpoints."""
import json
import logging
import math
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

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

API = 'https://acousticbrainz.org/api/v1'
USER_AGENT = 'yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )'
TIMEOUT_S = 20
MIN_INTERVAL = .5
CACHE_SIZE = 512
POSITIVE_TTL = 86400
MISS_TTL = 3600
_CLASSIFIERS = {
    'danceability': 'danceable', 'mood_party': 'party', 'mood_aggressive': 'aggressive',
    'mood_happy': 'happy', 'mood_sad': 'sad', 'mood_relaxed': 'relaxed',
    'mood_acoustic': 'acoustic', 'voice_instrumental': 'instrumental',
}
_LOG = logging.getLogger(__name__)


def _number(value, *, probability=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('AcousticBrainz descriptor is not numeric') from None
    if isinstance(value, bool) or not math.isfinite(number) or probability and not 0 <= number <= 1:
        raise ValueError('AcousticBrainz descriptor is outside its numeric domain')


class AcousticBrainzIAO:
    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now, self.monotonic = now_fn, monotonic_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(MIN_INTERVAL)
        self._lock = threading.RLock()
        self._scope = ContextVar('acousticbrainz_access_scope', default=None)
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
            saved = state.integration_cooldown('acousticbrainz') if state else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    def low_level(self, mbid):
        return self.get(mbid, 'low-level')

    def high_level(self, mbid):
        return self.get(mbid, 'high-level')

    def get_url(self, url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'acousticbrainz.org'
                or parsed.query or parsed.fragment or not parsed.path.startswith('/api/v1/')):
            raise ValueError('Unsupported AcousticBrainz resource')
        parts = parsed.path.removeprefix('/api/v1/').split('/')
        if len(parts) != 2:
            raise ValueError('Unsupported AcousticBrainz resource')
        return self.get(*parts)

    @staticmethod
    def _validate(endpoint, data):
        if not isinstance(data, dict) or 'error' in data:
            raise ValueError('AcousticBrainz response is not descriptor data')
        sections = ('highlevel',) if endpoint == 'high-level' else ('rhythm', 'tonal', 'lowlevel')
        if not any(section in data for section in sections):
            raise ValueError('AcousticBrainz response has no descriptor sections')
        for section in sections:
            if section in data and not isinstance(data[section], dict):
                raise ValueError('AcousticBrainz descriptor section is malformed')
        for model in data.get('highlevel', {}).values():
            if not isinstance(model, dict) or ('all' in model and not isinstance(model['all'], dict)):
                raise ValueError('AcousticBrainz classifier response is malformed')
        if endpoint == 'high-level':
            for model, label in _CLASSIFIERS.items():
                probabilities = data['highlevel'].get(model, {}).get('all', {})
                if label in probabilities:
                    _number(probabilities[label], probability=True)
        else:
            for section, field in (('rhythm', 'bpm'), ('lowlevel', 'average_loudness'),
                                   ('lowlevel', 'dynamic_complexity')):
                if field in data.get(section, {}):
                    _number(data[section][field])
            for field in ('key_key', 'key_scale'):
                if field in data.get('tonal', {}):
                    value = data['tonal'][field]
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError('AcousticBrainz tonal descriptor is malformed')

    def _error(self, wait, *, outage=True):
        error = IntegrationUnavailable('acousticbrainz', wait.error, wait.retry_at, outage=outage)
        net.record_probe_failure(error)
        return error

    def get(self, mbid, endpoint):
        if (not isinstance(mbid, str) or not re.fullmatch(
                r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', mbid)
                or endpoint not in ('low-level', 'high-level')):
            raise ValueError('Unsupported AcousticBrainz resource')
        key = (mbid.lower(), endpoint)
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            cached = self._cache.get(key)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(key)
                _LOG.debug('AcousticBrainz endpoint=%s outcome=cache', endpoint)
                return deepcopy(cached[0])
            admitted = self.availability()
            wait, outage = max(((admitted, True), self._waits.get(key, (Availability(), False))),
                               key=lambda value: value[0].retry_at)
            if wait.ready(self.now()):
                self.pacer.wait()
                check_cancellable_work()
                admitted = self.availability()
                if admitted.retry_at > wait.retry_at:
                    wait, outage = admitted, True
            if not wait.ready(self.now()):
                error = self._error(wait, outage=outage)
                _LOG.debug('AcousticBrainz endpoint=%s outcome=deferred retry_at=%.3f',
                           endpoint, error.retry_at)
                if cached and cached[2]:
                    self._cache.move_to_end(key)
                    return deepcopy(cached[0])
                raise error
            request = urllib.request.Request(f'{API}/{key[0]}/{endpoint}',
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            started = self.monotonic()
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response:
                    data = json.load(response)
                self._validate(endpoint, data)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                # urlopen raises before entering the response context manager.
                # HTTPError owns that response body, including a confirmed 404.
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    data = {}
                else:
                    error = self._failed(exc, key)
                    _LOG.info('AcousticBrainz endpoint=%s outcome=retry retry_at=%.3f elapsed=%.3f',
                              endpoint, error.retry_at, self.monotonic() - started)
                    if cached and cached[2]:
                        self._cache.move_to_end(key)
                        return deepcopy(cached[0])
                    raise error from exc
            self._failures = 0
            self._availability = Availability()
            state = self._state()
            if state:
                state.clear_integration_cooldown('acousticbrainz', admitted.retry_at)
            self._waits.pop(key, None)
            useful = bool(data)
            self._cache[key] = (deepcopy(data), self.monotonic() +
                                (POSITIVE_TTL if useful else MISS_TTL), useful)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            _LOG.debug('AcousticBrainz endpoint=%s outcome=success elapsed=%.3f',
                       endpoint, self.monotonic() - started)
            return data

    def _failed(self, exc, key):
        now = self.now()
        outage = net.provider_outage(exc)
        message = (f'AcousticBrainz HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError)
                   else f'AcousticBrainz {type(exc).__name__}')
        retry_at = max(now + 30, net.retry_after(exc, now))
        if outage:
            self._failures += 1
            retry_at = max(retry_at, now + min(3600, 30 * 2 ** min(self._failures - 1, 7)))
            state = self._state()
            if state:
                retry_at = state.defer_integration('acousticbrainz', now=now,
                    retry_after=retry_at, error=message)['retry_at']
            self._availability = Availability(retry_at, message)
        wait = Availability(retry_at, message)
        self._waits[key] = (wait, outage)
        self._waits.move_to_end(key)
        while len(self._waits) > CACHE_SIZE:
            self._waits.popitem(last=False)
        return self._error(wait, outage=outage)
