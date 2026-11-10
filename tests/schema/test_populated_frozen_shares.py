"""Opt-in v105 guard installation on our retained isolated actual library."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core.migrations import current
from yt_playlist.core.store import Store


def test_actual_frozen_share_guard_preserves_captures_and_published_sequences(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply our retained isolated discovery-subjects.db')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    assert target.name=='discovery-subjects.db'
    store = Store(target)
    tables = ('albums','album_tracklists','album_track_occurrences','album_occurrence_sources',
              'album_item_observations','album_share_snapshots','album_share_items')
    def captured():
        return {t:sorted(map(tuple,store.conn.execute('SELECT * FROM '+t)),key=repr) for t in tables}
    try:
        previous = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert 104<=previous<=current.VERSION
        before = captured()
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic()-started
        assert captured()==before
        guarded = 0
        for row in store.conn.execute('SELECT s.id,t.expected_count FROM album_share_snapshots s '
                                      'JOIN album_tracklists t ON t.id=s.tracklist_id').fetchall():
            assert store.conn.execute('SELECT count(*) FROM album_share_items WHERE snapshot_id=?',(row[0],)).fetchone()[0]==row[1]
            if row[1]:
                with pytest.raises(sqlite3.IntegrityError,match='membership cannot extend'):
                    with store.conn:
                        store.conn.execute('INSERT INTO album_share_items '
                            'SELECT snapshot_id,?,tracklist_id,occurrence_id,entry_id,raw_source_ref '
                            'FROM album_share_items WHERE snapshot_id=? AND position=0',(row[1],row[0]))
                guarded += 1
        assert guarded>0 and captured()==before
        # Read actual occurrence sequences through the display repository too.
        releases = store.conn.execute("SELECT p.provider_id,t.expected_count FROM albums a "
            "JOIN album_tracklists t ON t.id=a.current_tracklist_id "
            "JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "ORDER BY t.expected_count DESC LIMIT 5").fetchall()
        for browse,count in releases:
            assert len(store.charts.album_tracks_detail(browse))==count
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        report = {'previous_version':previous,'current_version':current.VERSION,'upgrade_seconds':elapsed,
                  'sqlite':sqlite3.sqlite_version,'protected_rows':{t:len(v) for t,v in before.items()},
                  'guarded_shares':guarded,'published_releases_checked':len(releases)}
        (tmp_path/'frozen-shares-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,sort_keys=True))
    finally:
        store.conn.close()
