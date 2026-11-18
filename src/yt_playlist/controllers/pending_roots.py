"""Retain roots while a file capability proves the capture is alive."""
from contextlib import nullcontext
import sqlite3
import threading
import uuid

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.db_space import StorageCompacting


class PendingRoots:
    def __init__(self, files, *, admission, writer, retain, release, before_close):
        self.files, self.admission, self.writer = files, admission, writer
        self.retain, self.release, self.before_close = retain, release, before_close

    def capture(self, resolve, at, *, sql_only=False):
        identity = uuid.uuid4().hex
        marker = self.files.reserve(identity)
        try:
            with nullcontext() if sql_only else self.admission(blocking=False):
                with self.writer():
                    captured = resolve()
                    retained = self.retain(identity, captured, at)
        except BaseException:
            marker.close(retired=True)
            raise
        if not retained:
            marker.close(retired=True)
            return captured, None
        lifetime, closed = threading.Lock(), False

        def close():
            nonlocal closed
            with lifetime:
                if closed:
                    return
                retired = False
                try:
                    with self.admission(blocking=False):
                        self.release(identity)
                        retired = True
                except (WorkBudgetExceeded, WorkClosing, StorageCompacting):
                    pass  # An unlocked marker lets a later bounded reaper retire the root.
                except sqlite3.OperationalError as error:
                    if getattr(error, 'sqlite_errorcode', 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                finally:
                    marker.close()
                    closed = True
                    if self.before_close is not None:
                        self.before_close.remove(close)
                if retired:
                    marker.close(retired=True)
        if self.before_close is not None:
            self.before_close.append(close)
        return captured, close

    def reap(self, identities):
        for identity in identities:
            with self.files.claim(identity) as marker:
                if marker is not None:
                    self.release(identity)
                    marker.close(retired=True)
