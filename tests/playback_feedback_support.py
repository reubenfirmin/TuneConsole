"""Playback observations and real copy admission for isolated feedback checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import sqlite3
import threading

import pytest

from yt_playlist.core.db_rebuild import job


@contextmanager
def held_copy(store):
    """Hold the real copy writer, then interrupt before building a candidate.

    Calling the job directly leaves inbox replay to the test, so restart and
    bounded-prefix checks can observe every capture before any projection.
    """
    entered, release = threading.Event(), threading.Event()

    def progress(phase, detail):
        if phase == 'copy':
            entered.set()
            assert release.wait(15), 'Playback check did not release copy admission'
            raise InterruptedError('controlled playback copy interruption')

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(job.run, store, progress=progress)
        try:
            assert entered.wait(10), 'Copy did not acquire its writer'
            assert store.activity_paused
            assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 1
            with closing(sqlite3.connect(store._db_path, timeout=0)) as writer:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    writer.execute('BEGIN IMMEDIATE')
            yield
        finally:
            release.set()
            with pytest.raises(InterruptedError, match='controlled playback copy interruption'):
                pending.result(timeout=10)
    assert not store.activity_paused
    assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 0


def event(position):
    return {'kind': 'next', 'videoId': 'song', 'position': position, 'duration': 180}
