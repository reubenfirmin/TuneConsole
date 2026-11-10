"""Reporting reads current facts without transactions or long writer locks."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from yt_playlist.rec import experiments, scorecard
from yt_playlist.rec import scorecard_attribution
from yt_playlist.repos.scorecard_attribution import ScorecardFacts


def seed(store):
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    store.rec_ledger.record_served_impressions('shown', 'home', [{
        'key': 'song|artist', 'rank': 1, 'model_version': 'm',
        'provenance': {'policy_kind': 'live', 'ranker': 'cosine',
                       'candidate': {'video_id': 'video'},
                       'experiment': {'comparison_id': 'one', 'cohort': 'c', 'cluster': 'one'}},
    }], 100)
    return iid


@pytest.mark.parametrize('method', ['scorecard', 'trace', 'experiment'])
def test_reports_allow_writes_between_queries(tmp_path, monkeypatch, method, consumer_store):
    store = consumer_store(tmp_path / 'state.db')
    iid = seed(store)
    started, release = threading.Event(), threading.Event()
    original = ScorecardFacts.outcomes

    def paused(self, *args):
        started.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(ScorecardFacts, 'outcomes', paused)
    reports = {
        'scorecard': lambda: scorecard.recommendation_scorecard(store, 90, 110),
        'trace': lambda: scorecard.served_recommendation(store, 1)['outcomes'],
        'experiment': lambda: scorecard.experiment_report(store, 90, 110, 100000)['live']['c:cosine'],
    }
    try:
        with ThreadPoolExecutor(2) as threads:
            result = threads.submit(reports[method])
            try:
                assert started.wait(5)
                threads.submit(store.history.record_play_event, iid, 'song|artist', 'video', 120).result(2)
                assert not result.done()
            finally:
                release.set()
            assert result.result(5)['played'] == 1
        assert scorecard.recommendation_scorecard(store, 90, 110)['played'] == 1
    finally:
        release.set()
        store.conn.close()


@pytest.mark.parametrize('method', ['scorecard', 'trace', 'experiment'])
def test_reports_do_not_open_transactions(store, method):
    seed(store)
    reports = {
        'scorecard': lambda: scorecard.recommendation_scorecard(store, 90, 110),
        'trace': lambda: scorecard.served_recommendation(store, 1),
        'experiment': lambda: scorecard.experiment_report(store, 90, 110, 100000),
    }
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        reports[method]()
    finally:
        store.conn.set_trace_callback(None)
    assert not store.conn.in_transaction
    assert not any(statement.lstrip().upper().startswith(
        ('BEGIN', 'SAVEPOINT', 'COMMIT', 'ROLLBACK', 'RELEASE')) for statement in sql)


@pytest.mark.parametrize('change', ['play', 'allocation'])
def test_experiment_decision_checks_evidence_without_holding_report_lock(tmp_path, monkeypatch, change, consumer_store):
    store = consumer_store(tmp_path / 'state.db')
    iid = seed(store)
    started, release = threading.Event(), threading.Event()
    prepare = experiments._prepare_decision

    def paused(*args):
        prepared = prepare(*args)
        started.set()
        assert release.wait(5)
        return prepared

    monkeypatch.setattr(experiments, '_prepare_decision', paused)
    try:
        with ThreadPoolExecutor(2) as threads:
            result = threads.submit(experiments.decide, store, 'retire', 'c', 'Stop', 100000)
            try:
                assert started.wait(5)
                if change == 'play':
                    threads.submit(store.history.record_play_event, iid, 'song|artist', 'video', 120).result(2)
                else:
                    threads.submit(experiments.rec_params.set_param, store, 'ppr_ab_share', .25).result(2)
            finally:
                release.set()
            with pytest.raises(ValueError, match='evidence or experiment settings changed'):
                result.result(5)
        assert experiments.decisions(store) == []
        assert experiments.lifecycle(store)['state'] == 'live'
        assert experiments.lifecycle(store)['share'] == (.25 if change == 'allocation' else .5)
    finally:
        release.set()
        store.conn.close()


def test_empty_report_does_not_scan_events_or_catalog(store):
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        report = scorecard.recommendation_scorecard(store, 0, 30 * 86400)
    finally:
        store.conn.set_trace_callback(None)
    assert report['impressions'] == 0
    assert not any(f'FROM {table}' in statement for statement in sql for table in
                   ('player_events', 'play_events', 'rec_response_events', 'tracks', 'discovered_tracks'))


def test_repeated_exits_use_bulk_historical_lookup_and_index(store):
    iid = seed(store)
    store.conn.executemany(
        'INSERT INTO player_events(account_id,kind,video_id,position,duration,at) VALUES (?,?,?,?,?,?)',
        [(iid, 'ended', 'video', 190, 200, at) for at in range(120, 320)])
    store.conn.commit()
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        report = scorecard.recommendation_scorecard(store, 90, 110)
        plan = store.conn.execute(
            'EXPLAIN QUERY PLAN SELECT identity_key FROM rec_served_impressions '
            f'WHERE {scorecard_attribution.VIDEO}=? AND {scorecard_attribution.LIVE} '
            'AND served_at>=? AND served_at<=?', ('video', 0, 1000)).fetchall()
    finally:
        store.conn.set_trace_callback(None)
    assert report['completion'] == 1
    lookups = [statement for statement in sql
               if 'FROM rec_served_impressions' in statement and not statement.startswith('EXPLAIN')]
    assert len(lookups) <= 3  # report, ownership and distinct-video history, regardless of exit count
    assert any('SEARCH' in row[3] and 'ix_rec_served_video_at' in row[3] for row in plan)
