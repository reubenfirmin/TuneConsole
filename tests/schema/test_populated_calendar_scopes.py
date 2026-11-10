"""Opt-in typed calendar scope indexing on the retained isolated actual library."""
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import listening_calendar
from yt_playlist.core.migrations import current
from yt_playlist.core.store import Store


def test_actual_calendar_scope_indexes_preserve_evidence_and_lookup_results(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply our retained isolated discovery-subjects.db')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    assert target.name=='discovery-subjects.db'
    store = Store(target)
    tables = ('play_events','play_event_sources','play_event_calendar','listening_observations','listening_calendars')
    def captured():
        return {t:sorted(map(tuple,store.conn.execute('SELECT * FROM '+t)),key=repr) for t in tables}
    try:
        previous = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert previous in (105,106)
        before = captured()
        sequences = dict(store.conn.execute('SELECT name,seq FROM sqlite_sequence'))
        scopes = defaultdict(set)
        for account,key,day,timezone in store.conn.execute('SELECT DISTINCT o.account_id,o.identity_key,o.source_day,o.timezone '
            'FROM listening_observation_subjects o JOIN listening_calendars c ON c.account_id=o.account_id '
            "WHERE o.precision='calendar_day' AND o.timezone<>c.timezone ORDER BY o.account_id,o.id LIMIT 20"):
            scopes[account].add((key,day,timezone))
        expected = {}
        for account,affected in scopes.items():
            expected[account] = sorted(tuple(row) for row in store.conn.execute(
                'SELECT o.id,o.identity_key,o.source_day,o.timezone,o.day FROM listening_observation_subjects o '
                'JOIN listening_calendars c ON c.account_id=o.account_id WHERE o.account_id=? '
                "AND o.precision='calendar_day' AND o.timezone<>c.timezone",(account,))
                if (row['identity_key'],row['source_day'],row['timezone']) in affected)
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic()-started
        assert captured()==before
        assert dict(store.conn.execute('SELECT name,seq FROM sqlite_sequence'))==sequences
        for account,affected in scopes.items():
            assert sorted(map(tuple,listening_calendar._foreign_rows(store.conn,account,affected,None)))==expected[account]
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        sizes = dict(store.conn.execute("SELECT name,sum(pgsize) FROM dbstat WHERE name IN "
            "('ix_listening_observation_unresolved_scope','ix_play_event_account_unresolved_time') GROUP BY name"))
        assert len(sizes)==2
        report = {'previous_version':previous,'current_version':current.VERSION,'upgrade_seconds':elapsed,
                  'sqlite':sqlite3.sqlite_version,'protected_rows':{t:len(v) for t,v in before.items()},
                  'account_scopes_checked':{str(a):len(s) for a,s in scopes.items()},'index_bytes':sizes}
        (tmp_path/'calendar-scopes-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,sort_keys=True))
    finally:
        store.conn.close()
