"""Reporting remains exact when diagnostic traces are unavailable or expired."""
import json

import pytest

from yt_playlist.core import policy_documents, served_facts as facts
from yt_playlist.rec import scorecard


def offer(store, request, at=100, *, shadow=False, controlled=True):
    trace = {'policy_kind': 'counterfactual' if shadow else 'live', 'ranker': 'ppr',
             'propensity': .5, 'pool_policy': 42,
             'candidate': {'artist': 'Captured artist', 'genre': 'Captured genre', 'ownership': 'cold',
                           'video_id': 'video', 'last_played': None,
                           'coverage': {'genre': True, 'year': False, 'audio': True}},
             'experiment': {'comparison_id': request, 'cluster': request, 'cohort': 'c',
                            'ranker_controlled': controlled, 'ppr_policy': {'algorithm': 'unknown-legacy-cache'}},
             'diagnostic': {'arbitrary': 'large explanation' * 100}}
    store.rec_ledger.record_served_impressions(request, 'explore', [{
        'identity_key': 'song|artist', 'rank': 1, 'lane': 'discovery', 'model_version': 'missing',
        'provenance': trace}], at)
    return trace


def activate(store):
    while not facts.active(store.conn):
        with store.conn:
            facts.prepare_batch(store.conn, limit=1)


def reports(store):
    return (scorecard.recommendation_scorecard(store, 90, 200),
            scorecard.experiment_report(store, 90, 200, 100000),
            store.rec_ledger.lane_impression_counts())


def test_partial_preparation_continuing_writes_and_expiry_preserve_reports(store, monkeypatch):
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    first = offer(store, 'first')
    offer(store, 'shadow', shadow=True)
    store.history.record_play_event(iid, 'song|artist', 'video', 160)
    store.player_events.record_player_event(iid, 'ended', 'video', 190, 200, None, None, 170)
    baseline = reports(store)
    assert baseline[0]['played'] == baseline[0]['completion'] == 1
    with store.conn:
        assert facts.prepare_batch(store.conn, limit=1) == 1
    assert not facts.active(store.conn)
    assert reports(store) == baseline
    offer(store, 'late', at=180, controlled='true')
    baseline = reports(store)
    activate(store)
    assert reports(store) == baseline
    assert scorecard.served_recommendation(store, 1)['provenance'] == first
    # Prepared reports must never load external traces, including attribution.
    monkeypatch.setattr(facts, 'diagnostic', lambda *args: pytest.fail('report opened diagnostic JSON'))
    assert reports(store) == baseline
    with store.conn:
        facts.expire(store.conn, facts.RETAIN_SECONDS + 201)
    assert reports(store) == baseline


def test_compact_writes_deduplicate_and_keep_policy_subject_facts(store):
    activate(store)
    trace = offer(store, 'one')
    offer(store, 'one')
    assert store.conn.execute('SELECT count(*) FROM served_facts').fetchone()[0] == 1
    row = store.conn.execute('SELECT * FROM rec_served_impressions').fetchone()
    assert json.loads(row['provenance']) == {'pool_policy': 42}
    assert row['raw_identity_key'] == 'song|artist' and row['recording_id'] is None
    links = store.conn.execute('SELECT kind,raw_version,resolution_reason FROM recommendation_impression_policies '
                               'ORDER BY kind').fetchall()
    assert [tuple(r) for r in links] == [('pool', '42', 'missing_policy'), ('primary', 'missing', 'missing_policy')]
    assert scorecard.served_recommendation(store, row['id'])['provenance'] == trace
    with store.conn:
        facts.expire(store.conn, facts.RETAIN_SECONDS + 101)
    expired = scorecard.served_recommendation(store, row['id'])
    assert expired['diagnostic_status'] == 'expired'
    assert expired['provenance']['candidate']['genre'] == 'Captured genre'
    assert expired['configuration'] is None
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_missing_corrupt_diagnostic_are_errors_but_reports_work(store):
    activate(store)
    offer(store, 'one')
    baseline = reports(store)
    path = facts.directory(store.conn) / facts.relative(1)
    encoded = path.read_bytes()
    path.unlink()
    with pytest.raises(ValueError, match='Unexpectedly missing'):
        scorecard.served_recommendation(store, 1)
    assert reports(store) == baseline
    path.write_bytes(encoded.replace(b'large explanation', b'wrong explanation', 1))
    with pytest.raises(ValueError, match='differs'):
        scorecard.served_recommendation(store, 1)


def test_idle_retention_defers_for_reader_and_keeps_monotonic_ids(store):
    activate(store)
    offer(store, 'one')
    path = facts.directory(store.conn) / facts.relative(1)
    now = facts.RETAIN_SECONDS + 101
    with policy_documents.retained_reader(facts.directory(store.conn)):
        store.rec_ledger.maintain_impressions(now)
        assert path.exists()
        assert store.conn.execute('SELECT count(*) FROM served_facts').fetchone()[0] == 1
    store.rec_ledger.maintain_impressions(now)
    assert not path.exists()
    assert store.conn.execute('SELECT count(*) FROM served_facts').fetchone()[0] == 0
    offer(store, 'next', at=now)
    assert store.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0] == 2


def test_failed_file_publication_rolls_back_and_can_retry(store, monkeypatch):
    activate(store)
    original = facts.publish
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(facts, 'publish', fail)
        with pytest.raises(OSError, match='interrupted'):
            offer(store, 'one')
    assert not store.conn.execute('SELECT 1 FROM rec_served_impressions').fetchone()
    assert not store.conn.execute('SELECT 1 FROM served_facts').fetchone()
    offer(store, 'one')
    assert scorecard.served_recommendation(store, 1)['diagnostic_status'] == 'available'


def test_backup_checks_exact_ownership_and_restores_trace(store, tmp_path):
    activate(store)
    trace = offer(store, 'one')
    manifest = facts.export(store.conn, tmp_path / 'backup')
    path = facts.directory(store.conn) / facts.relative(1)
    path.unlink()
    facts.restore(store.conn, manifest)
    assert scorecard.served_recommendation(store, 1)['provenance'] == trace
    backup = tmp_path / 'backup' / facts.relative(1)
    backup.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='verification'):
        facts.restore(store.conn, manifest)
