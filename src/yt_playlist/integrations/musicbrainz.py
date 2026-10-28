"""The sole MusicBrainz HTTP boundary, shared by every application caller.

The request scope supplies persistence explicitly without binding a process-wide
vendor object to one Store. No database lock spans pacing or network I/O.
"""
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
from copy import deepcopy

from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

API = 'https://musicbrainz.org/ws/2'
USER_AGENT = 'yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )'
TIMEOUT_S = 20
CACHE_SIZE = 1024
POSITIVE_TTL = 86400
MISS_TTL = 3600
_LOG = logging.getLogger(__name__)


def _text(value):
    return value is None or isinstance(value, str)


def _list(data, field, valid):
    value = data.get(field)
    return field not in data or isinstance(value, list) and all(valid(item) for item in value)


def _identity(value):
    return (isinstance(value, str) and bool(value)
            and all(c.isascii() and (c.isalnum() or c == '-') for c in value))


def _named(data):
    return isinstance(data, dict) and isinstance(data.get('name'), str)


def _artist(data):
    return (_named(data) and (data.get('id') is None or _identity(data['id']))
            and _list(data, 'aliases', _named))


def _credit(data):
    if isinstance(data, str):
        return True  # Older responses can carry a bare join phrase.
    return (isinstance(data, dict) and _text(data.get('name'))
            and _text(data.get('joinphrase'))
            and (data.get('artist') is None or _artist(data['artist']))
            and (isinstance(data.get('name'), str) or isinstance(data.get('artist'), dict)))


def _tag(data):
    if not _named(data):
        return False
    count = data.get('count')
    if count is None or type(count) is int:
        return True
    if not isinstance(count, str):
        return False
    try:
        int(count)
        return True
    except ValueError:
        return False


def _release(data):
    return (isinstance(data, dict) and (data.get('id') is None or _identity(data['id']))
            and _text(data.get('date'))
            and (data.get('release-group') is None
                 or isinstance(data['release-group'], dict)
                 and _text(data['release-group'].get('first-release-date'))))


def _relation(data):
    return (isinstance(data, dict) and _text(data.get('type'))
            and (data.get('url') is None or isinstance(data['url'], dict)
                 and _text(data['url'].get('resource'))))


def _entity(data, kind, *, search=False):
    if (not isinstance(data, dict) or not _identity(data.get('id'))
            or not isinstance(data.get('name' if kind == 'artist' else 'title'), str)):
        return False
    if kind == 'artist':
        return _artist(data) and all(_list(data, field, _tag) for field in ('genres', 'tags'))
    return (('artist-credit' in data and isinstance(data['artist-credit'], list) or not search)
            and _list(data, 'artist-credit', _credit)
            and _text(data.get('first-release-date'))
            and _list(data, 'releases', _release)
            and _list(data, 'isrcs', lambda value: isinstance(value, str))
            and all(_list(data, field, _tag) for field in ('genres', 'tags'))
            and _list(data, 'relations', _relation))


def _validate(path, data):
    if not isinstance(data, dict) or 'error' in data:
        raise ValueError('MusicBrainz response is not a successful object')
    kind, _, identity = path.partition('/')
    if identity:
        # Merged MBIDs can resolve to the surviving entity's canonical ID.
        # Validate the envelope here; domain matching stays with the adapter.
        valid = _entity(data, kind)
    else:
        field = {'recording': 'recordings', 'artist': 'artists', 'release-group': 'release-groups'}[kind]
        valid = isinstance(data.get(field), list) and all(
            _entity(row, kind, search=True) for row in data[field])
    if not valid:
        raise ValueError('MusicBrainz response has invalid ' + kind + ' metadata')


class MusicBrainzIAO:
    """Own availability, pacing, transport, response sharing and retry admission."""

    configured = True  # Public metadata access requires no credentials.

    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now = now_fn
        self.monotonic = monotonic_fn
        self.open = open_fn  # Resolve the installed HTTP/egress wrapper at request time.
        self.pacer = pacer or RateLimiter(1.1)
        self._lock = threading.RLock()
        self._scope = ContextVar('musicbrainz_access_scope', default=None)
        self._availability = Availability()
        self._failures = 0
        self._cache = OrderedDict()
        self._waits = OrderedDict()

    @contextmanager
    def context(self, state=None):
        # A nested helper without a Store inherits its caller's explicit scope.
        token = self._scope.set(state) if state is not None else None
        try:
            yield self
        finally:
            if token is not None:
                self._scope.reset(token)

    def availability(self):
        with self._lock:
            state = self._scope.get()
            saved = state.integration_cooldown('musicbrainz') if state else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    def _deferred(self, availability, *, outage=True):
        error = IntegrationUnavailable('musicbrainz', availability.error or
                                       'MusicBrainz is backing off', availability.retry_at,
                                       outage=outage)
        net.record_probe_failure(error)
        return error

    @staticmethod
    def _useful(path, data):
        if path in ('recording', 'artist', 'release-group'):
            field = {'recording': 'recordings', 'artist': 'artists',
                     'release-group': 'release-groups'}[path]
            return bool(data.get(field))
        if path.startswith('artist/'):
            return any(tag.get('name') for field in ('genres', 'tags')
                       for tag in data.get(field) or [])
        return bool(data)

    def get(self, path, params, *, timeout=TIMEOUT_S):
        kind, separator, identity = path.partition('/')
        if (kind not in ('recording', 'artist', 'release-group')
                or separator and not _identity(identity)):
            raise ValueError('Unsupported MusicBrainz endpoint')
        # The full request defines cache identity; matching happens on detached
        # responses, so a caller cannot mutate another caller's search evidence.
        key = (path, tuple(sorted(params.items())))
        started = self.monotonic()
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            cached = self._cache.get(key)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(key)
                _LOG.debug('MusicBrainz access endpoint=%s outcome=cache', path.split('/')[0])
                return deepcopy(cached[0])
            availability = self.availability()
            wait, outage = max(((availability, True), (self._waits.get(key, Availability()), False)),
                               key=lambda item: item[0].retry_at)
            if not wait.ready(self.now()):
                error = self._deferred(wait, outage=outage)
                if cached and cached[2]:
                    return deepcopy(cached[0])
                raise error
            self.pacer.wait()
            check_cancellable_work()
            # A different caller/process can establish a persisted wait while
            # this request is pacing. Recheck before touching the vendor.
            availability = self.availability()
            if not availability.ready(self.now()):
                error = self._deferred(availability)
                if cached and cached[2]:
                    return deepcopy(cached[0])
                raise error
            request = urllib.request.Request(API + '/' + path + '?' + urllib.parse.urlencode(params),
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            state = self._scope.get()
            try:
                _LOG.debug('MusicBrainz request endpoint=%s phase=start', path.split('/')[0])
                with (self.open or urllib.request.urlopen)(request, timeout=timeout) as response:
                    data = json.load(response)
                _validate(path, data)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    raise
                now = self.now()
                outage = net.provider_outage(exc)
                message = ('MusicBrainz HTTP ' + str(exc.code) if isinstance(exc, urllib.error.HTTPError)
                           else 'MusicBrainz ' + type(exc).__name__)
                if outage:
                    self._failures += 1
                    retry_at = max(now + min(3600, 30 * 2 ** min(self._failures - 1, 7)),
                                   net.retry_after(exc, now))
                    if state:
                        saved = state.defer_integration('musicbrainz', now=now,
                            retry_after=retry_at, error=message)
                        retry_at = saved['retry_at']
                    self._availability = Availability(retry_at, message)
                else:
                    retry_at = now + 30
                    self._waits[key] = Availability(retry_at, message)
                    self._waits.move_to_end(key)
                    while len(self._waits) > CACHE_SIZE:
                        self._waits.popitem(last=False)
                error = IntegrationUnavailable('musicbrainz', message, retry_at, outage=outage)
                net.record_probe_failure(error)
                _LOG.info('MusicBrainz request endpoint=%s outcome=retry retry_at=%.3f elapsed=%.3f',
                          path.split('/')[0], retry_at, self.monotonic() - started)
                if cached and cached[2]:
                    return deepcopy(cached[0])
                raise error from exc
            self._failures = 0
            self._availability = Availability()
            self._waits.pop(key, None)
            if state:
                state.clear_integration_cooldown('musicbrainz', availability.retry_at)
            useful = self._useful(path, data)
            self._cache[key] = (deepcopy(data), self.monotonic() +
                                (POSITIVE_TTL if useful else MISS_TTL), useful)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            _LOG.debug('MusicBrainz request endpoint=%s outcome=success elapsed=%.3f',
                       path.split('/')[0], self.monotonic() - started)
            return data
