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
from concurrent.futures import Future, TimeoutError as FuturesTimeout

logger = logging.getLogger(__name__)


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self):
        self._send = None          # async callable(frame) -> None
        self._loop = None          # asyncio loop the sender runs on
        self._conn_id = None       # identifies the current connection; guards stale disconnects
        self._conn_ids = itertools.count(1)
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self.now_playing = None    # {"title", "artist"} pushed by the extension, or None

    @property
    def connected(self) -> bool:
        return self._send is not None

    def connect(self, send_coro_fn, loop) -> int:
        with self._lock:
            self._conn_id = next(self._conn_ids)
            self._send = send_coro_fn
            self._loop = loop
            return self._conn_id

    def disconnect(self, conn_id: int | None = None) -> None:
        with self._lock:
            if conn_id is not None and conn_id != self._conn_id:
                # A stale/older socket tearing down after a newer one connected must not nuke
                # the current connection.
                return
            self._send = None
            self._loop = None
            self._conn_id = None
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(BridgeError("bridge disconnected"))

    def resolve(self, req_id: int, status: int, text: str) -> None:
        with self._lock:
            fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result((status, text))

    def execute(self, method: str, url: str, body: dict | None, timeout: float = 30.0):
        with self._lock:
            if self._send is None:
                raise BridgeError("no extension connected")
            send, loop = self._send, self._loop
            req_id = next(self._ids)
            fut: Future = Future()
            self._pending[req_id] = fut
        frame = {"id": req_id, "method": method, "url": url, "body": body}
        try:
            asyncio.run_coroutine_threadsafe(send(frame), loop)
        except Exception as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise BridgeError(f"bridge request {req_id} failed to send: {e}")
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeout:
            with self._lock:
                self._pending.pop(req_id, None)
            raise BridgeError(f"bridge request {req_id} timed out after {timeout}s")

    def send_control(self, payload: dict) -> None:
        """Fire-and-forget: push an unsolicited control frame to the extension (e.g. a navigate
        request). No response is expected, so nothing is registered in _pending."""
        with self._lock:
            if self._send is None:
                raise BridgeError("no extension connected")
            send, loop = self._send, self._loop
        asyncio.run_coroutine_threadsafe(send(payload), loop)
