"""In-process bridge between the synchronous Python egress and the browser extension.

A worker thread (the sync job) calls execute() and blocks. The frame is sent over the extension's
WebSocket, which lives on the asyncio loop, via run_coroutine_threadsafe. The WS receive loop calls
resolve() when the reply arrives. The credential never passes through here: frames carry only
{method, url, body}; the extension applies auth itself.
"""
import asyncio
import itertools
import logging
import threading
import time
from concurrent.futures import Future, TimeoutError as FuturesTimeout

from yt_playlist.core.activity import check_cancellable_work

logger = logging.getLogger(__name__)


class BridgeError(Exception):
    def __init__(self, message, *, outcome='uncertain'):
        super().__init__(message)
        self.outcome = outcome


class BridgeReply(tuple):
    """Keep the two-value transport API; only Retry-After may cross from headers."""
    def __new__(cls, status, text, headers=None):
        reply = super().__new__(cls, (status, text))
        reply.headers = {}
        if isinstance(headers, dict):
            for key, value in headers.items():
                if (isinstance(key, str) and key.lower() == 'retry-after'
                        and isinstance(value, str) and len(value) <= 256
                        and '\r' not in value and '\n' not in value):
                    reply.headers['Retry-After'] = value
        return reply


class Bridge:
    def __init__(self):
        self._send = None          # async callable(frame) -> None
        self._loop = None          # asyncio loop the sender runs on
        self._conn_id = None       # identifies the current connection; guards stale disconnects
        self._conn_ids = itertools.count(1)
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._playback_intent = 0
        self.now_playing = None    # {"title", "artist"} pushed by the extension, or None
        self.now_playing_seen_at = None  # monotonic heartbeat; expires stale state after a tab vanishes
        self.resumable_player = None  # last owned player; paused controls survive throttled heartbeats
        self.sensor_health = None  # latest extension probe of YTM tab/content-script health
        self.source_timezone = None  # browser timezone for YouTube's local-day history shelves

    @property
    def connected(self) -> bool:
        return self._send is not None

    @property
    def connection_id(self):
        """Changes on reconnect so failed reads can retry before their backoff expires."""
        return self._conn_id

    def connect(self, send_coro_fn, loop) -> int:
        with self._lock:
            self._conn_id = next(self._conn_ids)
            self._send = send_coro_fn
            self._loop = loop
            self.source_timezone = None
            return self._conn_id

    def disconnect(self, conn_id: int | None = None) -> bool:
        """Clear presence with the owned transport; report whether teardown was applied."""
        with self._lock:
            if conn_id is not None and conn_id != self._conn_id:
                # A stale/older socket tearing down after a newer one connected must not nuke
                # the current connection.
                return False
            self._send = None
            self._loop = None
            self._conn_id = None
            self.source_timezone = None
            self.now_playing = None
            self.now_playing_seen_at = None
            self.resumable_player = None
            self.sensor_health = None
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(BridgeError("bridge disconnected"))
        return True

    def resolve(self, req_id: int, status: int, text: str, source_timezone=None,
                response_headers=None) -> None:
        with self._lock:
            fut = self._pending.pop(req_id, None)
            if fut is not None:
                self.source_timezone = source_timezone
        if fut is not None and not fut.done():
            fut.set_result(BridgeReply(status, text, response_headers))

    def execute_read(self, method, url, body, timeout=30.0):
        """An IAO-classified disposable read may stop while awaiting its reply."""
        return self.execute(method, url, body, timeout, cancellable=True)

    def execute(self, method: str, url: str, body: dict | None, timeout: float = 30.0,
                *, cancellable=False):
        check_cancellable_work()
        with self._lock:
            if self._send is None:
                raise BridgeError("no extension connected", outcome='not-dispatched')
            send, loop = self._send, self._loop
            req_id = next(self._ids)
            fut: Future = Future()
            self._pending[req_id] = fut
        frame = {"id": req_id, "method": method, "url": url, "body": body}
        try:
            sending = asyncio.run_coroutine_threadsafe(send(frame), loop)
        except Exception:
            with self._lock:
                self._pending.pop(req_id, None)
            raise BridgeError('bridge request could not be queued', outcome='not-dispatched') from None

        def sent(result):
            # A coroutine failure used to leave the request waiting its entire
            # timeout. Dispatch may already have occurred, so never replay it.
            if result.cancelled() or result.exception() is not None:
                with self._lock:
                    pending = self._pending.pop(req_id, None)
                if pending is not None and not pending.done():
                    pending.set_exception(BridgeError('bridge request failed to send'))

        sending.add_done_callback(sent)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    return fut.result(timeout=max(0, min(.1, deadline - time.monotonic())))
                except FuturesTimeout:
                    if cancellable:
                        check_cancellable_work()
                    if time.monotonic() >= deadline:
                        raise BridgeError(f'bridge request {req_id} timed out after {timeout}s') from None
        finally:
            # Cancellation stops disposable reads promptly. It does not resend
            # anything or assert that an already-dispatched write failed.
            with self._lock:
                self._pending.pop(req_id, None)

    def begin_playback_intent(self):
        """Reserve playback order when the user starts an operation, before remote work."""
        with self._lock:
            self._playback_intent += 1
            return self._playback_intent

    def playback_intent_current(self, intent):
        with self._lock:
            return intent == self._playback_intent

    def send_control(self, payload: dict, *, intent=None) -> bool:
        """Fire-and-forget: push an unsolicited control frame to the extension (e.g. a navigate
        request). No response is expected, so nothing is registered in _pending."""
        with self._lock:
            if payload.get("type") == "navigate":
                if intent is None:
                    self._playback_intent += 1
                elif intent != self._playback_intent:
                    return False
            if self._send is None:
                raise BridgeError("no extension connected")
            send, loop = self._send, self._loop
            # Queue under the intent lock: an older handoff cannot pass its check,
            # then submit after a newer navigation has already been queued.
            asyncio.run_coroutine_threadsafe(send(payload), loop)
            return True

    async def request_control(self, payload: dict, timeout: float = 5.0):
        """Await a control acknowledgement while leaving the WebSocket response pump free."""
        with self._lock:
            if self._send is None:
                raise BridgeError("no extension connected")
            send, loop = self._send, self._loop
            req_id = next(self._ids)
            fut = Future()
            self._pending[req_id] = fut
        try:
            sending = asyncio.run_coroutine_threadsafe(send({**payload, "id": req_id}), loop)
            await asyncio.wait_for(asyncio.wrap_future(sending), timeout)
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.TimeoutError:
            raise BridgeError("player command timed out") from None
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
