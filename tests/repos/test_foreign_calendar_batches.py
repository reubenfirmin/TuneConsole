"""Foreign-date reconciliation visits affected evidence once per ingestion batch."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from yt_playlist.core import listening, listening_calendar


def stamp(local, timezone='UTC'):
    return dt.datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(timezone)).timestamp()


def account(store, timezone='UTC'):
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    with store.conn:
        listening.calendar(store.conn, iid, timezone)
    return iid


@pytest.mark.parametrize('size', [20, 80, 250])
def test_history_batch_reconciles_once_with_one_exact_lookup_per_scope(store, monkeypatch, size):
    iid = account(store)
    at = stamp('2026-10-20T12:00:00')
    keys = [f'song-{i}' for i in range(size)]
    store.history.import_listening_events(iid, [(key, None, at) for key in keys])
    calls = []
    original = listening.reconcile_foreign_days

    def reconcile(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(listening, 'reconcile_foreign_days', reconcile)
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        added = store.history.record_history_plays(iid, at, [(key, 'Today') for key in keys],
                                                  source_timezone='America/Los_Angeles')
    finally:
        store.conn.set_trace_callback(None)
    assert added == 0
    assert len(calls) == 1
    assert len(calls[0]['observations']) == size
    lookups = [statement for statement in sql if statement.startswith('SELECT pe.played_at,c.day')]
    assert len(lookups) == size
    assert all('pe.played_at>=' in statement and 'pe.played_at<' in statement for statement in lookups)
    assert store.history.listening_day_counts(iid) == dict.fromkeys(keys, 1)


def test_same_scope_reuses_lookup_across_sources_and_skips_unchanged_projection(store):
    iid = account(store)
    at = stamp('2026-10-20T12:00:00')
    store.history.import_listening_events(iid, [('song', None, at)])
    scope = 'song', '2026-10-20', 'America/Los_Angeles'
    with store.conn:
        for source in ('independent', 'ytm_history'):
            listening.observe_day(store.conn, iid, *scope[:2], source, scope[2], at, reconcile=False)
        listening.reconcile_foreign_days(store.conn, iid, observations={scope})
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        with store.conn:
            listening.reconcile_foreign_days(store.conn, iid, observations={scope})
    finally:
        store.conn.set_trace_callback(None)
    assert sum(statement.startswith('SELECT pe.played_at,c.day') for statement in sql) == 1
    assert not any(statement.startswith('UPDATE listening_observations SET day=') for statement in sql)
    assert {row[0] for row in store.conn.execute('SELECT source FROM listening_observations')} == {
        'independent', 'ytm_history'}
    assert store.history.listening_day_counts(iid) == {'song': 1}


@pytest.mark.parametrize('ingest', ['live', 'takeout', 'spotify'])
def test_exact_ingestion_revisits_only_affected_recording_and_source_date(store, monkeypatch, ingest):
    iid = account(store, 'America/Costa_Rica')
    at = stamp('2026-10-21T05:30:00')
    with store.conn:
        for key, day in [('song', '2026-10-20'), ('song', '2026-10-21'), ('other', '2026-10-21')]:
            listening.observe_day(store.conn, iid, key, day, 'ytm_history', 'UTC', at, reconcile=False)
    visited = []
    original = listening_calendar._foreign_rows

    def rows(*args):
        for row in original(*args):
            visited.append((row['identity_key'], row['source_day']))
            yield row

    monkeypatch.setattr(listening_calendar, '_foreign_rows', rows)

    def record(timestamp):
        if ingest == 'live':
            store.history.record_play_event(iid, 'song', None, timestamp)
        elif ingest == 'takeout':
            store.history.import_listening_events(iid, [('song', None, timestamp)])
        else:
            store.history.import_spotify_listens(iid, [('spotify:song', timestamp, 'Artist', 'song')])

    record(at)
    assert visited == [('song', '2026-10-21')]
    assert store.conn.execute("SELECT day FROM listening_observations WHERE identity_key='song' "
                              "AND source_day='2026-10-21'").fetchone()[0] == '2026-10-20'
    visited.clear()
    record(at + 7200)
    assert visited == [('song', '2026-10-21')]
    # Two account dates now anchor the same UTC date: the coarse projection clears.
    assert all(row[0] is None for row in store.conn.execute('SELECT day FROM listening_observations'))
    assert store.history.listening_day_counts(iid) == {'song': 2}


@pytest.mark.parametrize('date,day_hours', [('2026-03-08', 23), ('2026-11-01', 25)])
def test_exact_lookup_uses_real_dst_day_bounds(store, monkeypatch, date, day_hours):
    iid = account(store)
    start = stamp(date + 'T00:00:00', 'America/New_York')
    end = start + day_hours * 3600
    store.history.import_listening_events(iid, [('song', None, at) for at in
                                             (start - 1, start + 3600, end)])
    scope = 'song', date, 'America/New_York'
    converted = []
    original = listening_calendar.day_at

    def day_at(at, timezone):
        converted.append(at)
        return original(at, timezone)

    monkeypatch.setattr(listening_calendar, 'day_at', day_at)
    with store.conn:
        listening.observe_day(store.conn, iid, *scope[:2], 'ytm_history', scope[2], start, reconcile=False)
        listening.reconcile_foreign_days(store.conn, iid, observations={scope})
    assert converted == [start + 3600]
    assert store.conn.execute('SELECT day FROM listening_observations').fetchone()[0] == date


def test_observation_scope_plan_uses_account_recording_source_date_index(store):
    iid = account(store)
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        listening.reconcile_foreign_days(store.conn, iid,
                                         observations={('song', '2026-10-20', 'UTC')})
    finally:
        store.conn.set_trace_callback(None)
    query = next(statement for statement in sql if statement.startswith('WITH affected'))
    plan = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN ' + query)]
    assert any('SEARCH o USING INDEX ix_listening_observation_source_scope' in detail
               and 'identity_key=?' in detail and 'source_day=?' in detail and 'timezone=?' in detail
               for detail in plan)
