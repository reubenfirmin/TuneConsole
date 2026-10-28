"""Public latest-release access; installation and version policy live elsewhere."""
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

RELEASES_URL = 'https://api.github.com/repos/reubenfirmin/TuneConsole/releases/latest'
USER_AGENT = 'yt-playlist-updatecheck'
TIMEOUT_S = 8
CACHE_TTL = 60
MAX_RESPONSE_BYTES = 1024 * 1024
_LOG = logging.getLogger(__name__)


class GitHubReleasesIAO:
    """Serialize public release reads and share one bounded result and cooldown."""

    configured = True

    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic, open_fn=None, pacer=None):
        self.now, self.monotonic = now_fn, monotonic_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(1.0)
        self._lock = threading.RLock()
        self._scope = ContextVar('github_release_scope', default=None)
        self._availability = Availability()
        self._failures = 0
        self._cached = None

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
            saved = state.integration_cooldown('github') if state is not None else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    @staticmethod
    def _reset_at(headers, now):
        if not headers or headers.get('X-RateLimit-Remaining') != '0':
            return 0
        try:
            reset = float(headers.get('X-RateLimit-Reset'))
            return reset if math.isfinite(reset) and reset > now else 0
        except (ValueError, TypeError):
            return 0

    def _defer(self, retry_at, message):
        state = self._scope.get()
        if state is not None:
            retry_at = state.defer_integration('github', now=self.now(),
                retry_after=retry_at, error=message)['retry_at']
        self._availability = Availability(retry_at, message)
        return self._availability

    def _unavailable(self, wait):
        error = IntegrationUnavailable('github', wait.error or 'GitHub release access is backing off',
                                       wait.retry_at)
        net.record_probe_failure(error)
        if self._cached and self._cached[0] is not None:
            return deepcopy(self._cached[0])
        raise error

    @staticmethod
    def _decode(response):
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('GitHub release response exceeds the byte limit')
        data = json.loads(raw)
        if (not isinstance(data, dict) or 'message' in data or 'error' in data
                or not isinstance(data.get('tag_name'), str) or not data['tag_name'].strip()
                or len(data['tag_name']) > 256
                or any(field in data and data[field] is not False for field in ('draft', 'prerelease'))):
            raise ValueError('GitHub latest-release response is invalid')
        # Nothing else in the release response is needed by update consumers.
        return {'tag_name': data['tag_name']}

    def latest_release(self):
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            if self._cached and self.monotonic() < self._cached[1]:
                return deepcopy(self._cached[0])
            availability = self.availability()
            if not availability.ready(self.now()):
                return self._unavailable(availability)
            self.pacer.wait()
            check_cancellable_work()
            availability = self.availability()
            if not availability.ready(self.now()):
                return self._unavailable(availability)
            request = urllib.request.Request(RELEASES_URL,
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'}, method='GET')
            headers = None
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response:
                    headers = getattr(response, 'headers', None)
                    data = self._decode(response)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    data = None  # The public repository has no latest release.
                else:
                    self._failures += 1
                    now = self.now()
                    message = ('GitHub release HTTP ' + str(exc.code)
                               if isinstance(exc, urllib.error.HTTPError)
                               else 'GitHub release ' + type(exc).__name__)
                    wait = self._defer(max(now + min(3600, 60 * 2 ** min(self._failures - 1, 6)),
                        net.retry_after(exc, now), self._reset_at(getattr(exc, 'headers', None), now)), message)
                    _LOG.info('GitHub latest-release outcome=retry retry_at=%.3f', wait.retry_at)
                    return self._unavailable(wait)
            state = self._scope.get()
            if state is not None:
                state.clear_integration_cooldown('github', availability.retry_at)
            self._failures = 0
            self._availability = Availability()
            self._cached = (deepcopy(data), self.monotonic() + CACHE_TTL)
            reset_at = self._reset_at(headers, self.now())
            if reset_at:
                self._defer(reset_at, 'GitHub public request budget exhausted')
            _LOG.debug('GitHub latest-release outcome=success')
            return data
