"""YouTube Music access policy; browser credentials stay in the extension.

The SDK owns endpoint construction/parsing, and domain services own completeness,
write journals and reconciliation. This boundary admits each request exactly once.
It never retries a request, including reads: a lost mutation response is ambiguous.
"""
import json
import threading
import time
import weakref
from collections import OrderedDict
from urllib.parse import urlsplit

import requests

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.bridge import BridgeError
from yt_playlist.integrations.base import Availability, IntegrationUnavailable
from yt_playlist.util.net import retry_after

MIN_INTERVAL = .1  # Local pacing policy, not an advertised YouTube quota.
MAX_IN_FLIGHT = 4
ADMISSION_TIMEOUT = 1
SESSION_RETRY_S = 30
TRANSPORT_RETRY_S = 5
_ACCESSES = weakref.WeakKeyDictionary()
_ACCESS_LOCK = threading.Lock()


class YouTubeMusicUnavailable(BridgeError, IntegrationUnavailable):
    def __init__(self, message, retry_at, *, outcome='not-dispatched', mutation=False):
        IntegrationUnavailable.__init__(self, 'youtube_music', message, retry_at)
        self.outcome = outcome
        self.mutation = mutation


def for_bridge(bridge):
    """All fresh SDK clients for one bridge share admission and vendor waits."""
    if bridge is None:  # Runtime prepares clients before the bridge is wired.
        return YouTubeMusicIAO(None)
    with _ACCESS_LOCK:
        access = _ACCESSES.get(bridge)
        if access is None:
            access = _ACCESSES[bridge] = YouTubeMusicIAO(bridge)
        return access


class YouTubeMusicIAO:
    def __init__(self, bridge, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 sleep_fn=time.sleep, min_interval=MIN_INTERVAL):
        self._bridge = weakref.ref(bridge) if bridge is not None else lambda: None
        self.now, self.monotonic, self.sleep = now_fn, monotonic_fn, sleep_fn
        self.min_interval = min_interval
        self._lock = threading.RLock()
        self._host = Availability()
        self._transport = (None, Availability())
        self._sessions = OrderedDict()
        self._mutations = OrderedDict()
        self._revision = self._failures = self._in_flight = 0
        self._next_slot = 0

    @staticmethod
    def operation(method, url, body=None):
        parts = urlsplit(url)
        if (method not in {'GET', 'POST'} or parts.scheme != 'https'
                or parts.netloc.lower() != 'music.youtube.com' or parts.fragment
                or not parts.path.startswith('/youtubei/v1/')
                or any(segment in {'.', '..', ''} for segment in parts.path.split('/')[3:])
                or '%' in parts.path or '\\' in parts.path):
            raise ValueError('YouTube Music request is outside the browser API boundary')
        endpoint = parts.path.removeprefix('/youtubei/v1/')
        # Unknown POSTs remain mutations. In particular browse/edit_playlist and
        # feedback must never inherit browse's read classification.
        reads = {'browse', 'search', 'next', 'player', 'music/get_search_suggestions',
                 'account/account_menu'}
        return method == 'POST' and (endpoint not in reads
            or endpoint == 'browse' and isinstance(body, dict) and 'formData' in body)

    def availability(self, identity=None, *, mutation=False):
        with self._lock:
            bridge = self._bridge()
            connection = getattr(bridge, 'connection_id', None)
            waits = [self._host]
            if self._transport[0] == connection:
                waits.append(self._transport[1])
            session = self._sessions.get((connection, identity))
            if session:
                waits.append(session[1])
            if mutation:
                waits.append(self._mutations.get((connection, identity), Availability()))
            return max(waits, key=lambda wait: wait.retry_at)

    @staticmethod
    def _response(url, status, text, headers=None):
        response = requests.Response()
        response.status_code, response.url, response.encoding = status, url, 'utf-8'
        response._content = text.encode('utf-8')
        response.reason = {401: 'Unauthorized', 403: 'Forbidden'}.get(
            status, 'OK' if status < 400 else 'Error')
        response.headers.update(headers or {})
        return response

    def _admit(self, identity, mutation, url):
        deadline = self.monotonic() + ADMISSION_TIMEOUT
        while True:
            # Retain the application's foreground priority and cancellation scopes.
            check_cancellable_work()
            with self._lock:
                now = self.now()
                bridge = self._bridge()
                connection = getattr(bridge, 'connection_id', None)
                if bridge is None or not getattr(bridge, 'connected', True):
                    raise YouTubeMusicUnavailable('no extension connected', now + TRANSPORT_RETRY_S,
                                                   mutation=mutation)
                # Genuine session failures keep their HTTP status so sync's
                # re-auth path stays distinct from disconnected/no-tab states.
                session = self._sessions.get((connection, identity))
                if session and session[1].retry_at > now:
                    message = 'not signed in' if session[0] == 401 else 'YouTube Music denied the request'
                    return self._response(url, session[0], json.dumps({'error': {'message': message}}))
                wait = self.availability(identity, mutation=mutation)
                if wait.retry_at > now:
                    raise YouTubeMusicUnavailable(wait.error, wait.retry_at, mutation=mutation)
                tick = self.monotonic()
                if self._in_flight < MAX_IN_FLIGHT and tick >= self._next_slot:
                    self._in_flight += 1
                    self._next_slot = tick + self.min_interval
                    return bridge, connection, self._revision
                if tick >= deadline:
                    raise YouTubeMusicUnavailable('YouTube Music admission is busy', now + .1,
                                                   mutation=mutation)
                delay = min(.05, deadline - tick)
            # No state/database lock spans pacing or a browser request.
            self.sleep(delay)

    def _session_failure(self, connection, identity, status):
        with self._lock:
            self._revision += 1
            self._sessions[(connection, identity)] = (
                status, Availability(self.now() + SESSION_RETRY_S, 'not signed in'))
            self._sessions.move_to_end((connection, identity))
            while len(self._sessions) > 128:
                self._sessions.popitem(last=False)

    def _transport_failure(self, connection, identity=None, *, mutation=False, outcome='uncertain'):
        with self._lock:
            self._revision += 1
            wait = Availability(self.now() + TRANSPORT_RETRY_S,
                                'extension could not reach YouTube Music')
            bridge = self._bridge()
            if mutation and outcome == 'uncertain' and getattr(bridge, 'connected', False):
                # Permit the domain's safe readback after a lost write reply.
                # Further writes wait; the IAO never repeats the ambiguous one.
                self._mutations[(connection, identity)] = wait
                self._mutations.move_to_end((connection, identity))
                while len(self._mutations) > 128:
                    self._mutations.popitem(last=False)
            elif connection == getattr(bridge, 'connection_id', None):
                self._transport = connection, wait
            return wait

    def request(self, method, url, body=None, *, identity=None):
        mutation = self.operation(method, url, body)
        admitted = self._admit(identity, mutation, url)
        if isinstance(admitted, requests.Response):
            return admitted
        bridge, connection, revision = admitted
        try:
            try:
                dispatch = bridge.execute if mutation else getattr(bridge, 'execute_read', bridge.execute)
                reply = dispatch(method, url, body)
            except BridgeError as exc:
                outcome = getattr(exc, 'outcome', 'uncertain')
                wait = self._transport_failure(connection, identity, mutation=mutation, outcome=outcome)
                raise YouTubeMusicUnavailable('YouTube Music bridge request failed', wait.retry_at,
                    outcome=outcome, mutation=mutation) from None
            status, text = reply
            headers = getattr(reply, 'headers', {})
            if (not isinstance(status, int) or status != 0 and not 100 <= status <= 599
                    or not isinstance(text, str)):
                wait = self._transport_failure(connection, identity, mutation=mutation)
                raise YouTubeMusicUnavailable('Invalid YouTube Music bridge response', wait.retry_at,
                                               outcome='uncertain', mutation=mutation)
            if status == 0:
                try:
                    error = json.loads(text)
                except (ValueError, TypeError):
                    error = {}
                if not isinstance(error, dict):
                    error = {}
                if error.get('error') == 'not signed in':
                    self._session_failure(connection, identity, 401)
                    return self._response(url, 401, json.dumps({'error': {'message': 'not signed in'}}))
                outcome = 'not-dispatched' if error.get('outcome') == 'not-dispatched' else 'uncertain'
                wait = self._transport_failure(connection, identity, mutation=mutation, outcome=outcome)
                raise YouTubeMusicUnavailable(wait.error, wait.retry_at,
                                               outcome=outcome, mutation=mutation)
            response = self._response(url, status, text, headers)
            with self._lock:
                if status == 429 or status >= 500:
                    self._revision += 1
                    self._failures += 1
                    retry_at = max(self.now() + min(300, 30 * 2 ** min(self._failures - 1, 4)),
                                   retry_after(response, self.now()), self._host.retry_at)
                    self._host = Availability(retry_at, f'YouTube Music HTTP {status}')
                elif status in {401, 403}:
                    self._session_failure(connection, identity, status)
                elif status < 400 and self._revision == revision:
                    # An earlier in-flight success cannot erase a newer failure.
                    self._host, self._failures = Availability(), 0
                    self._transport = connection, Availability()
                    self._sessions.pop((connection, identity), None)
            return response
        finally:
            with self._lock:
                self._in_flight -= 1


class YouTubeStoryboardIAO:
    """Bounded public sprite downloads, separate from account/API availability."""
    MAX_BYTES = 2_000_000

    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic, get_fn=None):
        self.now, self.monotonic, self.get = now_fn, monotonic_fn, get_fn
        self._lock = threading.Lock()
        self._wait = Availability()

    def _defer(self, retry_at, error):
        with self._lock:
            # Overlapping requests can finish in either order. Keep the reason
            # belonging to the longest applicable wait, including equal deadlines.
            if retry_at > self._wait.retry_at:
                self._wait = Availability(retry_at, error)
            return self._wait

    def read(self, url, deadline):
        from yt_playlist.egress import storyboard_url_allowed
        if not storyboard_url_allowed(url):
            raise ValueError('Invalid YouTube storyboard URL')
        check_cancellable_work()
        with self._lock:
            wait = self._wait
        if wait.retry_at > self.now():
            raise IntegrationUnavailable('youtube_storyboards', wait.error, wait.retry_at)
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError('TV preview deadline exceeded')
        try:
            with (self.get or requests.get)(url, timeout=(min(3, remaining), min(4, remaining)),
                    stream=True, allow_redirects=False) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    retry_at = max(self.now() + 30, retry_after(response, self.now()))
                    wait = self._defer(retry_at, 'YouTube storyboard service is unavailable')
                    raise IntegrationUnavailable('youtube_storyboards', wait.error, wait.retry_at)
                if response.status_code != 200 or not response.headers.get('Content-Type', '').startswith('image/'):
                    raise ValueError('No timeline image')
                data = bytearray()
                for chunk in response.iter_content(65536):
                    check_cancellable_work()
                    data.extend(chunk)
                    if len(data) > self.MAX_BYTES or self.monotonic() >= deadline:
                        raise ValueError('Timeline image exceeded its budget')
                return bytes(data)
        except requests.RequestException:
            wait = self._defer(self.now() + 5, 'YouTube storyboard transport failed')
            raise IntegrationUnavailable('youtube_storyboards', wait.error, wait.retry_at) from None


storyboards = YouTubeStoryboardIAO()
