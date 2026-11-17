"""Bounded, expiring, single-use in-memory source snapshots."""
import secrets
import threading


class TransferPreviews:
    def __init__(self, ttl=7200, capacity=8):
        self.ttl, self.capacity = ttl, capacity
        self._entries = {}
        self._lock = threading.Lock()

    def _expire(self, now):
        for token, (deadline, _) in list(self._entries.items()):
            if deadline <= now:
                del self._entries[token]

    def put(self, value, now):
        with self._lock:
            self._expire(now)
            while len(self._entries) >= self.capacity:
                del self._entries[next(iter(self._entries))]
            token = secrets.token_urlsafe(32)
            self._entries[token] = (now + self.ttl, value)
            return token

    def get(self, token, now, *, consume=False):
        with self._lock:
            self._expire(now)
            entry = self._entries.pop(token, None) if consume else self._entries.get(token)
            return entry[1] if entry else None
