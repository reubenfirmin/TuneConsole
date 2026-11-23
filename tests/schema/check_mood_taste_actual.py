"""Compare installed mood and taste records on a private upgraded snapshot."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core.migrations.mood_taste_storage import RETIRED
from yt_playlist.core.store import Store


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_installed_mood_events_and_checkpoint():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as previous:
        before = tables(previous)
        keys = {}
        for event, _, key in previous.execute('''SELECT f.mood_event_id,f.position,r.identity_key
            FROM mood_event_recordings f JOIN recordings r ON r.id=f.recording_id
            UNION ALL SELECT mood_event_id,position,legacy_key FROM mood_event_unresolved_seeds ORDER BY 1,2'''):
            keys.setdefault(event, []).append(key)
        events = [(at, direction, keys.get(event, [])) for event, at, direction in previous.execute(
            'SELECT id,created_at,direction FROM mood_events ORDER BY created_at DESC,id DESC')]
        fields = 'position,axis_id,captured_axis,weight,updated_at'
        weights = list(previous.execute(f'SELECT {fields} FROM taste_checkpoint_weights ORDER BY position'))
        assert events and weights

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert store.rec_signals.recent_mood_events(limit=len(events)) == events
        checkpoint = store.rec_model.repository.checkpoint_facts()
        assert [tuple(row[name] for name in fields.split(',')) for row in checkpoint['weights']] == weights
        assert store.rec_model.restore_taste_settings([])
        expected = store.rec_model.repository.checkpoint_axes([row[0] for row in weights])
        assert list(map(tuple, conn.execute('SELECT id,weight,weight_updated_at FROM taste_axes '
            'WHERE weight IS NOT NULL ORDER BY id'))) == sorted(expected)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn):
        store.init_schema()
        assert store.rec_signals.recent_mood_events(limit=len(events)) == events
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before-after),
        mood_events=len(events), mood_seeds=sum(len(row[2]) for row in events), exact_checkpoint_weights=len(weights),
        restored_weights=len(expected), reopened=True, foreign_key_errors=0)
    report_file('mood-taste-c184.json').write_text(json.dumps(report, indent=2) + '\n')
