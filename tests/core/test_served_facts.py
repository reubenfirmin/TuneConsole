"""Reporting remains exact when diagnostic traces are unavailable or expired."""
import json
import hashlib
import time
import threading
import sqlite3
from contextlib import closing

import pytest

from yt_playlist.core import fao, policy_documents, served_facts as facts
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.served_files import ServedFiles
from yt_playlist.rec import scorecard
from yt_playlist.controllers.recommendation_ledger import RESPONSE_RETAIN_DAYS


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


def reports(store):
    return (scorecard.recommendation_scorecard(store, 90, 200),
            scorecard.experiment_report(store, 90, 200, 100000),
            store.rec_ledger.lane_impression_counts())


def test_reports_preserve_captured_attribution_after_diagnostic_expiry(store, monkeypatch):
    iid = store.accounts.upsert_account('main', None, True)
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    first = offer(store, 'first')
    offer(store, 'shadow', shadow=True)
    store.history.record_play_event(iid, 'song|artist', 'video', 160)
    store.player_events.record_player_event(iid, 'ended', 'video', 190, 200, None, None, 170)
    baseline = reports(store)
    assert baseline[0]['played'] == baseline[0]['completion'] == 1
    assert scorecard.served_recommendation(store, 1)['provenance'] == first
    files = store.rec_ledger.impressions.files
    monkeypatch.setattr(files, 'diagnostic', lambda *args: pytest.fail('report opened diagnostic JSON'))
    assert reports(store) == baseline
    store.rec_ledger.maintain_impressions(facts.RETAIN_SECONDS + 201)
    assert reports(store) == baseline


def test_foreground_write_completes_while_diagnostic_file_is_staging(file_backed_store, monkeypatch):
    from yt_playlist.core.store import Store
    store = file_backed_store
    entered, written = threading.Event(), threading.Event()
    traces, failures = [], []
    stage = fao.prepare
    def delayed(*args, **kwargs):
        entered.set()
        assert written.wait(5), 'File preparation retained the SQL writer'
        return stage(*args, **kwargs)
    monkeypatch.setattr(fao, 'prepare', delayed)
    def analytics():
        prepared = Store.open_current(store._db_path)
        try:
            traces.append(offer(prepared, 'first'))
        except BaseException as error:
            failures.append(error)
        finally:
            prepared.conn.close()
    worker = threading.Thread(target=analytics)
    worker.start()
    try:
        assert entered.wait(5)
        store.settings.set_setting('during-diagnostic-compression', 'accepted')
        written.set()
    finally:
        written.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    assert store.settings.get_setting('during-diagnostic-compression') == 'accepted'
    assert scorecard.served_recommendation(store, 1)['provenance'] == traces[0]


def test_compact_writes_deduplicate_and_keep_policy_subject_facts(store):
    trace = offer(store, 'one')
    offer(store, 'one')
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions WHERE policy_kind IS NOT NULL').fetchone()[0] == 1
    row = store.conn.execute('SELECT * FROM rec_served_impressions').fetchone()
    assert json.loads(row['provenance']) == {'pool_policy': 42}
    assert row['raw_identity_key'] == 'song|artist' and row['recording_id'] is None
    assert row['model_version'] == 'missing'
    assert row['primary_policy_reason'] == row['pool_policy_reason'] == 'missing_policy'
    assert scorecard.served_recommendation(store, row['id'])['provenance'] == trace
    store.rec_ledger.maintain_impressions(facts.RETAIN_SECONDS + 101)
    expired = scorecard.served_recommendation(store, row['id'])
    assert expired['diagnostic_status'] == 'expired'
    assert expired['provenance']['candidate']['genre'] == 'Captured genre'
    assert expired['configuration'] is None
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_missing_corrupt_diagnostic_are_errors_but_reports_work(store):
    offer(store, 'one')
    baseline = reports(store)
    path = facts.directory(store.conn) / facts.relative(1)
    encoded = fao.read(path).bytes
    path.unlink()
    with pytest.raises(ValueError, match='Unexpectedly missing'):
        scorecard.served_recommendation(store, 1)
    assert reports(store) == baseline
    path.write_bytes(encoded.replace(b'large explanation', b'wrong explanation', 1))
    with pytest.raises(ValueError, match='differs'):
        scorecard.served_recommendation(store, 1)


def test_idle_retention_defers_for_reader_and_keeps_monotonic_ids(store):
    offer(store, 'one')
    path = facts.directory(store.conn) / facts.relative(1)
    now = max(facts.RETAIN_SECONDS, RESPONSE_RETAIN_DAYS * 86400) + 101
    with policy_documents.retained_reader(facts.directory(store.conn)):
        store.rec_ledger.maintain_impressions(now)
        assert path.exists()
        assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 1
    store.rec_ledger.maintain_impressions(now)
    assert not path.exists()
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 0
    offer(store, 'next', at=now)
    assert store.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0] == 2


def test_file_scan_recovers_interrupted_expiry(store, monkeypatch):
    now = time.time()
    offer(store, 'kept-for-report', at=now)
    path = facts.directory(store.conn) / facts.relative(1)
    files = store.rec_ledger.impressions.files
    cutoff = now + facts.RETAIN_SECONDS + 10

    def fail(_):
        raise OSError('interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(files, 'delete_many', fail)
        with pytest.raises(OSError, match='interrupted'):
            store.rec_ledger.maintain_impressions(cutoff)
    assert path.exists()
    assert store.rec_ledger.impressions.facts(1)['diagnostic_status'] == 'expired'
    store.rec_ledger.maintain_impressions(cutoff)
    assert not path.exists()


def test_orphan_scan_skips_disappeared_file_and_keeps_later_candidates(tmp_path, monkeypatch):
    files = ServedFiles(tmp_path / 'served')
    files.publish_many([(1, 100, {}), (2, 100, {})])
    missing = files.root / facts.relative(1)
    remaining = files.root / facts.relative(2)
    original = fao.read
    expected = original(remaining).info.identity

    def remove_before_stat(path):
        if path == missing:
            missing.unlink()
        return original(path)

    monkeypatch.setattr(fao, 'read', remove_before_stat)
    assert set(files.orphan_candidates(time.time() + 10, limit=256)) == {(2, expected)}
    assert not missing.exists()
    assert remaining.is_file()


@pytest.mark.parametrize('retry', (True, False))
def test_failed_file_publication_keeps_allocated_ids_and_recovers(store, monkeypatch, retry):
    files = store.rec_ledger.impressions.files
    original = files.publish_many
    def fail(rows):
        assert not store.conn.in_transaction
        original(rows)
        raise OSError('interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(files, 'publish_many', fail)
        with pytest.raises(OSError, match='interrupted'):
            offer(store, 'one')
    assert not store.conn.execute('SELECT 1 FROM rec_served_impressions').fetchone()
    assert store.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='rec_served_impressions'").fetchone()[0] == 1
    path = facts.directory(store.conn) / facts.relative(1)
    assert path.exists()
    if retry:
        offer(store, 'one')
        assert scorecard.served_recommendation(store, 2)['diagnostic_status'] == 'available'
        assert path.exists()  # Failed ID 1 remains an unowned file until expiry.
    else:
        now = time.time() + facts.RETAIN_SECONDS + 100
        with policy_documents.retained_reader(facts.directory(store.conn)):
            store.rec_ledger.maintain_impressions(now)
            assert path.exists()
        # Collection rediscovers the file after the interrupted publication.
        store.rec_ledger.maintain_impressions(time.time())
        # A fresh file accessor resumes its bounded cursor after restart.
        store.rec_ledger.impressions.files = ServedFiles(files.root)
        for _ in range(3):
            store.rec_ledger.maintain_impressions(now)
        assert not path.exists()


def test_failed_reporting_owner_write_keeps_id_and_file_for_bounded_orphan_collection(store):
    store.conn.execute("CREATE TEMP TRIGGER reject_served_fact BEFORE INSERT ON rec_served_impressions "
                       "BEGIN SELECT RAISE(ABORT,'late SQL failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='late SQL failure'):
        offer(store, 'one')
    assert not store.conn.in_transaction
    assert store.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='rec_served_impressions'").fetchone()[0] == 1
    assert not store.conn.execute('SELECT 1 FROM rec_served_impressions').fetchone()
    assert (facts.directory(store.conn) / facts.relative(1)).exists()
    store.conn.execute('DROP TRIGGER reject_served_fact')
    offer(store, 'one')
    assert scorecard.served_recommendation(store, 2)['diagnostic_status'] == 'available'


def test_new_diagnostic_has_a_committed_id_before_file_preparation(file_backed_store, monkeypatch):
    store = file_backed_store
    prepare = fao.prepare
    observed = []
    def checked(path, *args, **kwargs):
        assert not store.conn.in_transaction
        with closing(sqlite3.connect(store._db_path)) as observer:
            identity, = observer.execute("SELECT seq FROM sqlite_sequence WHERE name='rec_served_impressions'").fetchone()
            assert not observer.execute('SELECT 1 FROM rec_served_impressions').fetchone()
        assert path == facts.directory(store.conn) / facts.relative(identity)
        observed.append(identity)
        return prepare(path, *args, **kwargs)
    monkeypatch.setattr(fao, 'prepare', checked)
    offer(store, 'one')
    assert observed == [1]
    assert store.rec_ledger.impressions.facts(1)['diagnostic_status'] == 'available'


@pytest.mark.parametrize('damage', ('checksum', 'meaning', 'incomplete'))
def test_backup_checks_all_members_before_restoring_trace(store, tmp_path, damage):
    trace = offer(store, 'one')
    second = offer(store, 'two')
    manifest = facts.export(store.conn, tmp_path / 'backup')
    path = facts.directory(store.conn) / facts.relative(1)
    path.unlink()
    facts.restore(store.conn, manifest)
    assert scorecard.served_recommendation(store, 1)['provenance'] == trace
    before = {i: fao.read(facts.directory(store.conn) / facts.relative(i)).bytes for i in (1, 2)}
    document = json.loads(fao.read(manifest).bytes)
    # A valid earlier member with a different opaque diagnostic would be
    # accepted on its own. A bad later member must not publish that change.
    for i in (1, 2):
        backup = tmp_path/'backup'/facts.relative(i)
        envelope = json.loads(fao.read(backup).bytes)
        if i == 1:
            envelope['body']['provenance']['diagnostic'] = {'replacement': True}
        elif damage == 'meaning':
            envelope['body']['provenance']['candidate']['genre'] = 'Wrong genre'
        envelope['sha256'] = hashlib.sha256(canonical(envelope['body'])).hexdigest()
        encoded = canonical(envelope)
        fao.write(backup, encoded)
        document['entries'][i-1][1:] = [len(encoded), hashlib.sha256(encoded).hexdigest()]
    if damage == 'checksum':
        document['entries'][1][2] = 'wrong'
    elif damage == 'incomplete':
        document['entries'].pop()
    fao.write(manifest, canonical(document))
    with pytest.raises(ValueError, match='verification|captured owner|incomplete'):
        facts.restore(store.conn, manifest)
    assert {i: fao.read(facts.directory(store.conn) / facts.relative(i)).bytes for i in (1, 2)} == before
    assert scorecard.served_recommendation(store, 2)['provenance'] == second
