"""Priority coordination for rate-limited enrichment jobs.

Each playlist the user enriches runs as its own background job, but they all share one rate-limited
API (MusicBrainz, Last.fm). To make the *most recently requested* playlist win — its tracks jump the
queue — every job passes through a PriorityGate before each track: a job yields (at track boundaries)
while any newer job is still active. So clicking the icon on playlist B preempts an in-progress job
on playlist A; B runs to completion, then A resumes where it left off.

One gate per source (separate APIs don't contend with each other).
"""
import threading


class PriorityGate:
    def __init__(self):
        self._cond = threading.Condition()
        self._seq = 0
        self._active = set()

    def enter(self) -> int:
        """Register a job; returns its sequence number (higher = newer = higher priority)."""
        with self._cond:
            self._seq += 1
            self._active.add(self._seq)
            return self._seq

    def wait_turn(self, seq, on_wait=None) -> None:
        """Block while any newer job is active. on_wait() fires once if we actually wait."""
        with self._cond:
            notified = False
            while any(a > seq for a in self._active):
                if on_wait and not notified:
                    on_wait()
                    notified = True
                self._cond.wait()

    def leave(self, seq) -> None:
        with self._cond:
            self._active.discard(seq)
            self._cond.notify_all()       # let older jobs re-check and resume
