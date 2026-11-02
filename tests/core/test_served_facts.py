"""Reporting remains exact when diagnostic traces are unavailable or expired."""
import json
import hashlib
import time

import pytest

from yt_playlist.core import fao, policy_documents, served_facts as facts
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec import scorecard
from yt_playlist.repos.rec_ledger import RESPONSE_RETAIN_DAYS


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
    encoded = fao.read_bytes(path)
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
    now = max(facts.RETAIN_SECONDS, RESPONSE_RETAIN_DAYS * 86400) + 101
    with policy_documents.retained_reader(facts.directory(store.conn)):
        store.rec_ledger.maintain_impressions(now)
        assert path.exists()
        assert store.conn.execute('SELECT count(*) FROM served_facts').fetchone()[0] == 1
    store.rec_ledger.maintain_impressions(now)
    assert not path.exists()
    assert store.conn.execute('SELECT count(*) FROM served_facts').fetchone()[0] == 0
    offer(store, 'next', at=now)
    assert store.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0] == 2


@pytest.mark.parametrize('retry', (True, False))
def test_failed_file_publication_rolls_back_and_recovers(store, monkeypatch, retry):
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
    path = facts.directory(store.conn) / facts.relative(1)
    assert path.exists()
    if retry:
        offer(store, 'one')
        assert scorecard.served_recommendation(store, 1)['diagnostic_status'] == 'available'
    else:
        now = time.time() + facts.RETAIN_SECONDS + 100
        with policy_documents.retained_reader(facts.directory(store.conn)):
            store.rec_ledger.maintain_impressions(now)
            assert path.exists()
        # Collection must rediscover an orphan without a SQL retirement receipt.
        # Clear the in-memory traversal to exercise restart from its durable shard.
        store.rec_ledger.maintain_impressions(time.time())
        current = getattr(store.conn, '_served_orphan_gc', None)
        if current is not None and hasattr(current[1], 'close'):
            current[1].close()
            store.conn._close_callbacks.remove(current[1].close)
        store.conn._served_orphan_gc = None
        for _ in range(3):
            store.rec_ledger.maintain_impressions(now)
        assert not path.exists()


@pytest.mark.parametrize('damage', ('checksum', 'meaning', 'incomplete'))
def test_backup_checks_all_members_before_restoring_trace(store, tmp_path, damage):
    activate(store)
    trace = offer(store, 'one')
    second = offer(store, 'two')
    manifest = facts.export(store.conn, tmp_path / 'backup')
    path = facts.directory(store.conn) / facts.relative(1)
    path.unlink()
    facts.restore(store.conn, manifest)
    assert scorecard.served_recommendation(store, 1)['provenance'] == trace
    before = {i: fao.read_bytes(facts.directory(store.conn)/facts.relative(i)) for i in (1, 2)}
    document = json.loads(fao.read_bytes(manifest))
    # A valid earlier member with a different opaque diagnostic would be
    # accepted on its own. A bad later member must not publish that change.
    for i in (1, 2):
        backup = tmp_path/'backup'/facts.relative(i)
        envelope = json.loads(fao.read_bytes(backup))
        if i == 1:
            envelope['body']['provenance']['diagnostic'] = {'replacement': True}
        elif damage == 'meaning':
            envelope['body']['provenance']['candidate']['genre'] = 'Wrong genre'
        envelope['sha256'] = hashlib.sha256(canonical(envelope['body'])).hexdigest()
        encoded = canonical(envelope)
        fao.write_bytes(backup, encoded)
        document['entries'][i-1][1:] = [len(encoded), hashlib.sha256(encoded).hexdigest()]
    if damage == 'checksum':
        document['entries'][1][2] = 'wrong'
    elif damage == 'incomplete':
        document['entries'].pop()
    fao.write_bytes(manifest, canonical(document))
    with pytest.raises(ValueError, match='verification|captured owner|incomplete'):
        facts.restore(store.conn, manifest)
    assert {i: fao.read_bytes(facts.directory(store.conn)/facts.relative(i)) for i in (1, 2)} == before
    assert scorecard.served_recommendation(store, 2)['provenance'] == second
