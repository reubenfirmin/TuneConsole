"""Opt-in Home preparation progress check on a read-only backup of a saved library.

Run explicitly with `uv run pytest -s tests/bench_home_prepared.py`.
TUNECONSOLE_BENCH_DB can select another source database; only the copy is modified.
"""
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import home_prepared


def test_repeated_preparation_publishes_on_saved_library(tmp_path, caplog):
    source = Path(os.environ.get('TUNECONSOLE_BENCH_DB',
                                 str(Path.home() / '.local/share/yt-playlist/state.db')))
    if not source.exists():
        pytest.skip('No saved library; set TUNECONSOLE_BENCH_DB')
    destination = tmp_path / 'library.db'
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as reader:
        # A fixed read transaction prevents live writes from restarting the backup.
        reader.execute('BEGIN')
        reader.execute('SELECT MAX(version) FROM schema_migrations').fetchone()
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=1024)
    limit_native_threads()
    store = Store(destination)
    store.init_schema()
    caplog.set_level(logging.DEBUG, logger=home_prepared.__name__)
    now, attempts, published = time.time(), [], []
    try:
        # No source/model changes between attempts, and no previous serving cache to rescue us.
        for surface in (home_prepared.SURFACE, 'home_tuned'):
            store.rec_surface.put_proposals(surface, None, now)
        choice = store.rec_surface.get_proposals('home_choice')
        impressions = store.modes.impression_counts()
        for _ in range(3):
            started, cpu = time.monotonic(), time.thread_time()
            payload = home_prepared.prepare(store, now)
            attempts.append({'elapsed_s': time.monotonic() - started,
                             'cpu_s': time.thread_time() - cpu,
                             'menus': len(payload['menus']) if payload else 0})
            print(json.dumps(attempts[-1]), flush=True)
            assert attempts[-1]['elapsed_s'] < 3
            if payload:
                published.append(payload)
        assert published, attempts
        assert all(home_prepared._valid(payload) and payload['menus'][0]['protos'] for payload in published)
        assert store.rec_surface.get_proposals(home_prepared.SURFACE) == json.loads(json.dumps(published[-1]))
        assert store.rec_surface.get_proposals('home_choice') == choice
        assert store.modes.impression_counts() == impressions
    finally:
        store.conn.close()
