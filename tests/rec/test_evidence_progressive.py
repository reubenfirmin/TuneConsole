"""Retention yields durable prefixes to other jobs without fencing the app."""
from tests.file_content_support import read_bytes as original_bytes
from tests.file_content_support import patch_byte_reads
from tests.file_content_support import age_file
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.rec.test_evidence_responsiveness import evidence_template, library
from yt_playlist.core import fao, playback_feedback
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence, choice_retention
from yt_playlist.rec.worker_queue import Job
from yt_playlist.web.app import create_app


def test_ledger_inventory_commits_prefixes_across_restarts_and_continuing_arrivals(tmp_path, monkeypatch):
    ledger = EvidenceLedger(tmp_path, 10_000_000)
    artifacts = [ledger.artifact({'saved': str(index) * 50_000}) for index in range(23)]
    for path in tmp_path.glob('artifact/*/*.json'):
        age_file(path, mtime_ns=10 ** 9)
    read = original_bytes
    counts, turns, saved = {}, [], []

    def interrupted(path, **kwargs):
        if path.parent.parent.name == 'artifact':
            turns[-1] += 1
            if turns[-1] > 9:
                raise WorkBudgetExceeded('Controlled slice exhausted after nine large reads')
            counts[path.name] = counts.get(path.name, 0) + 1
        return read(path, **kwargs)

    patch_byte_reads(monkeypatch, interrupted)
    for turn in range(10):
        # Every turn reconstructs the cache from disk, with no old iterator.
        ledger = EvidenceLedger(tmp_path, ledger.byte_budget)
        turns.append(0)
        try:
            with work_budget(60):
                result = ledger.maintain(0, orphan_before=10)
        except WorkBudgetExceeded:
            cache = json.loads(read(tmp_path / '.retention-inventory'))
            saved.append(len(cache['entries']))
            assert len(list(tmp_path.glob('artifact/*/*.json'))) == 23
            if turn == 0:
                # A new durable choice protects a previously scanned graph.
                ledger.put('opportunity', 'arrived', {'id': 'arrived', 'context': artifacts[0], 'observed_at': 2})
                ledger.put('response', 'picked', {'id': 'picked', 'opportunity_id': 'arrived',
                                                  'kind': 'choice', 'observed_at': 3})
        else:
            break
    else:
        pytest.fail('Repeated slices never reached deletion')
    patch_byte_reads(monkeypatch, read)
    assert len(saved) >= 2 and all(b > a for a, b in zip(saved, saved[1:]))
    assert max(counts.values()) <= 2  # At most the uncommitted tail is reread.
    assert ledger.get('artifact', artifacts[0])
    assert len(ledger.records('artifact')) == 1
    assert result['used_bytes'] == int((tmp_path / '.budget').read_text())
    assert ledger.get('response', 'picked')


def test_cached_inventory_rechecks_sql_projections_and_renewed_artifact_leases(tmp_path):
    context = ['old']
    ledger = EvidenceLedger(tmp_path, 1_000_000,
                            opportunity_projection=lambda row: {**row, 'context': context[0]})
    old, new, renewed = [ledger.artifact({'name': name}) for name in ('old', 'new', 'renewed')]
    context[0] = old
    ledger.put('opportunity', 'offer', {'id': 'offer', 'context': old, 'observed_at': 20})
    for path in tmp_path.glob('artifact/*/*.json'):
        age_file(path, mtime_ns=10 ** 9)
    ledger.maintain(0, orphan_before=0)
    context[0] = new
    age_file(ledger._path('artifact', renewed), mtime_ns=100 * 10 ** 9)
    ledger.maintain(0, orphan_before=50)
    assert ledger.get('artifact', old) is None
    assert ledger.get('artifact', new) and ledger.get('artifact', renewed)


def test_publication_contention_cannot_outlast_the_workers_slice(tmp_path):
    ledger = EvidenceLedger(tmp_path, 1_000_000)

    def collect():
        with work_budget(.05):
            return ledger.maintain(0)

    with ThreadPoolExecutor(1) as executor:
        with ledger._budget():
            # Another publication owns the file reservation. Its lifetime must
            # not prevent this worker from returning to its next queued job.
            future = executor.submit(collect)
            with pytest.raises(WorkBudgetExceeded):
                future.result(timeout=1)
    # Both the reader fence and failed reservation were released for retry.
    assert not ledger.maintain(0).get('deferred')


def test_checkpoint_stage_uses_multiple_prefixes_then_resumes_after_restart(library, monkeypatch):
    for index in range(8):
        choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 20 + index, context={'at': 20 + index})
    monkeypatch.setattr(choice_retention, 'BATCH', 1)
    acknowledge, calls = choice_retention.acknowledge, []

    def cutoff(*args):
        calls.append(True)
        if len(calls) == 4:
            raise WorkBudgetExceeded('Controlled end of time slice')
        return acknowledge(*args)

    monkeypatch.setattr(choice_retention, 'acknowledge', cutoff)
    assert choice_evidence.maintain_storage(library, 100)['deferred']
    assert len(calls) == 4 and len(library.rec_ledger.evidence().records('checkpoint')) == 3
    assert library.settings.get_setting(choice_evidence.STORAGE_PHASE) == 'checkpoint'
    assert library.settings.get_setting(choice_retention.AFTER)
    assert not library.activity_paused
    monkeypatch.setattr(choice_retention, 'acknowledge', acknowledge)
    reopened = Store(library._db_path)
    try:
        reopened.init_schema()
        result = choice_evidence.maintain_storage(reopened, 100)
        assert not result.get('deferred')
        assert result['compacted_contexts'] == 9
        assert len(reopened.rec_ledger.evidence().records('checkpoint')) == 9
        assert reopened.settings.get_setting(choice_evidence.STORAGE_PENDING) == '0'
    finally:
        reopened.conn.close()


def test_reader_contention_keeps_retention_stage_and_allows_writes_and_feedback(library, monkeypatch):
    app = create_app(library, lambda: {}, background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    account = library.accounts.upsert_account('Main', 'credential', None, True)
    with library.read_view():
        assert choice_evidence.maintain_storage(library, 100)['deferred']
        assert library.settings.get_setting(choice_evidence.STORAGE_PHASE) == 'retention'
        assert library.settings.get_setting(choice_evidence.STORAGE_PENDING) == '1'
        playback_feedback.capture(library, 'player', account, {'kind': 'state', 'state': 'paused'}, 100)
        status = maintenance.status()
        assert status['pending_feedback'] == 1
        assert not status['active'] and status['recording'] and not library.activity_paused
        assert playback_feedback.drain(library) == 1
        with TestClient(app, base_url='http://127.0.0.1') as client:
            assert client.post('/settings/lastfm-key', data={'key': 'during-retention'}).status_code == 204
        assert library.settings.get_setting('lastfm_api_key') == 'during-retention'
    # A deferred reader lease does not restart checkpointing on the next turn.
    monkeypatch.setattr(choice_retention, 'acknowledge', lambda *a: pytest.fail('Checkpoint stage repeated'))
    assert not choice_evidence.maintain_storage(library, 100).get('deferred')
    assert not maintenance.status()['evidence_maintenance']['pending']


def test_expired_slice_returns_to_next_queued_worker_job(library, monkeypatch):
    app = create_app(library, lambda: {}, now_fn=lambda: 100, background_work=True)
    worker = app.state.ctx.rec_worker
    worker.debounce_s = 0
    events = []
    entered, release = threading.Event(), threading.Event()

    def retention(*args, **kwargs):
        events.append('retention')
        raise WorkBudgetExceeded('Controlled worker slice exhausted')

    def ratings():
        assert library.settings.get_setting(choice_evidence.STORAGE_PENDING) == '1'
        assert not library.activity_paused
        events.append('ratings')
        entered.set()
        assert release.wait(5)
        return set()

    monkeypatch.setattr(choice_evidence, '_maintain_storage', retention)
    monkeypatch.setattr(worker.tasks, 'ratings', ratings)
    worker.jobs.merge(Job.EVIDENCE_STORAGE)
    worker.jobs.merge(Job.RATINGS)
    worker._running = True
    try:
        with ThreadPoolExecutor(1) as executor:
            turn = executor.submit(worker._run_pending)
            try:
                assert entered.wait(5)
                status = app.state.ctx.storage_maintenance.status()
                assert not status['active'] and status['evidence_maintenance']['pending']
                library.settings.set_setting('during-next-job', 'saved')
                assert library.settings.get_setting('during-next-job') == 'saved'
            finally:
                release.set()
            turn.result(timeout=5)
        assert events == ['retention', 'ratings']
        assert not worker._running and not worker.jobs
        assert worker.jobs.retry[Job.EVIDENCE_STORAGE].retry_at > 0
    finally:
        worker.shutdown()


@pytest.mark.parametrize('unsafe', ['cutover', 'installed-64'])
def test_real_recovery_still_fences_writes(library, unsafe):
    app = create_app(library, lambda: {}, background_work=False)
    if unsafe == 'cutover':
        library.activity_paused = True
    else:
        library._rebuild_source_version = 64
    try:
        assert library.activity_paused and app.state.ctx.storage_maintenance.status()['active']
        with TestClient(app, base_url='http://127.0.0.1') as client:
            assert client.post('/settings/lastfm-key', data={'key': 'unsafe'}).status_code == 503
        assert library.settings.get_setting('lastfm_api_key') is None
    finally:
        library.activity_paused = False
        library._rebuild_source_version = None
