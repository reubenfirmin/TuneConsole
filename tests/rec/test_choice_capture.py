"""Listening capture must leave serving responsive and preserve the actual offered state."""
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity
from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence as choices, choice_outcomes
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.worker_queue import Job


@pytest.fixture
def capture(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 100), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    yield store, worker
    worker.shutdown()
    store.conn.close()


def test_capture_freezes_menu_and_observations_before_worker_writes(capture, monkeypatch):
    store, worker = capture
    calls = []
    put = EvidenceLedger.put

    def observed_put(ledger, *args, **kwargs):
        calls.append(threading.get_ident())
        return put(ledger, *args, **kwargs)

    monkeypatch.setattr(EvidenceLedger, 'put', observed_put)
    options = [{'id': 'a', 'score': .9}, {'id': 'b', 'score': .8}]
    context = {'at': 90, 'layers': {'now': {'events': 2}}}
    oid = choices.present(store, 'albums', options, 100, context=context, window=1)
    assert choices.present(store, 'albums', options, 101, context=context, window=1) == oid
    assert choices.respond(store, oid, 'visible', 102, visible=['a', 'b'])
    details = {'source': ['album']}
    assert choices.respond(store, oid, 'choice', 103, option_id='a', details=details)
    assert not calls
    assert len(worker.jobs.measurements) == 3
    assert choices.opportunity(store, oid)['at'] == 100
    # Confirmed playback can bind to a menu while its raw write is still queued.
    iid = store.identities.upsert_identity('main', 'test', None, True)
    choice_outcomes.bind(store, iid, 'PL', 'v1', oid, 'a', 104)
    assert choice_outcomes.observe(store, iid, 'PL', 'v1', 'ended', 180, 180, 1, 105)
    assert not calls
    options[0]['score'] = 999
    context['layers']['now']['events'] = 999
    details['source'].append('changed')
    with ThreadPoolExecutor(1) as thread:
        thread.submit(worker._flush_measurements).result(5)
    assert calls and all(ident != threading.get_ident() for ident in calls)
    assert not worker.jobs.measurements
    offer = choices.opportunity(store, oid)
    assert offer['options'][0]['score'] == .9
    assert choices.resolve_context(store, {'artifact': offer['context']})['layers']['now']['events'] == 2
    ledger = store.rec_ledger.evidence()
    responses = ledger.records('response')
    assert {row['kind'] for row in responses} == {'visible', 'choice', 'experience'}
    choice = next(row for row in responses if row['kind'] == 'choice')
    assert choice['at'] == 103 and choice['details'] == {'source': ['album']}
    # A rerender after persistence also retains the first observation time.
    choices.present(store, 'albums', offer['options'], 110,
                    context={'at': 90, 'layers': {'now': {'events': 2}}}, window=1)
    worker._flush_measurements()
    assert choices.opportunity(store, oid)['at'] == 100


def test_invalid_capture_does_not_drop_later_events(capture, caplog):
    store, worker = capture
    oid = choices.issue(store, 'mixes', [{'id': 'a'}], 100, context={})
    assert choices.respond(store, oid, 'choice', 99, option_id='a')
    assert choices.respond(store, oid, 'choice', 101, option_id='wrong')
    assert choices.respond(store, oid, 'choice', 102, option_id='a')
    worker._flush_measurements()
    events = store.rec_ledger.evidence().records('response')
    assert [(event['at'], event['option_id']) for event in events] == [(102, 'a')]
    assert 'Response precedes its opportunity' in caplog.text
    assert 'Response does not belong' in caplog.text


def test_close_resolves_the_held_offer_before_its_worker_write(capture):
    store, worker = capture
    first = choices.present(store, 'albums', [{'id': 'a'}], 100, context={}, window=1)
    second = choices.present(store, 'albums', [{'id': 'b'}], 101, context={}, window=2)
    assert not store.rec_ledger.evidence().records('opportunity')
    assert choices.close_window(store, 'albums', 102, opportunity_id=first)['id'] == first
    choices.close_window(store, 'albums', 103, opportunity_id=first)
    worker._flush_measurements()
    responses = store.rec_ledger.evidence().records('response')
    assert [(row['opportunity_id'], row['kind']) for row in responses] == [(first, 'close')]
    assert choices.opportunity(store, second)


def test_capture_backpressure_and_shutdown_do_not_invent_evidence(capture):
    store, worker = capture
    oid = choices.issue(store, 'albums', [{'id': 'a'}], 100, context={})
    for index in range(63):
        assert choices.respond(store, oid, 'open', 101, option_id='a', action_id=str(index))
    assert not choices.respond(store, oid, 'choice', 102, option_id='a')
    assert choices.present(store, 'albums', [{'id': 'b'}], 103, context={}, window=2) is None
    assert store.rec_proposals.get_proposals('choice_window:albums') is None
    worker.shutdown()
    assert store.choice_capture.get(oid) is None
    assert not store.rec_ledger.evidence().records('opportunity')
    assert choices.issue(store, 'albums', [{'id': 'a'}], 104, context={}) is None


def test_pending_offer_protects_its_context_during_retention(capture):
    store, worker = capture
    reference = choices.freeze_context(store, {'at': 50, 'layers': {'saved': {'version': 1}}})
    oid = choices.issue(store, 'albums', [{'id': 'a'}], 100, context=reference)
    choices.maintain_storage(store, 10 ** 12)
    assert store.rec_ledger.evidence().get('artifact', reference['artifact'])
    worker._flush_measurements()
    offer = choices.opportunity(store, oid)
    assert choices.resolve_context(store, {'artifact': offer['context']})['at'] == 50
    assert not store.choice_capture.artifacts()


def test_queued_response_protects_its_old_opportunity_until_worker_writes(capture):
    store, worker = capture
    oid = choices.issue(store, 'albums', [{'id': 'a'}], 100, context={'old': True})
    worker._flush_measurements()
    choices.respond(store, oid, 'choice', 101, option_id='a')
    assert oid in store.choice_capture.opportunities()
    choices.maintain_storage(store, 10**12)
    assert choices.opportunity(store, oid)
    worker._flush_measurements()
    assert store.rec_ledger.evidence().records('response')[0]['kind'] == 'choice'
    assert not store.choice_capture.opportunities()


def test_worker_recovers_capacity_once_before_releasing_capture(capture, monkeypatch):
    from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded
    store, worker = capture
    oid = choices.issue(store, 'albums', [{'id': 'a'}], 100, context={})
    original = EvidenceLedger.put
    attempts, maintenance = [], []

    def initially_full(ledger, kind, *args, **kwargs):
        if kind == 'opportunity':
            attempts.append(kind)
            if len(attempts) == 1:
                raise EvidenceBudgetExceeded('full')
        return original(ledger, kind, *args, **kwargs)

    monkeypatch.setattr(EvidenceLedger, 'put', initially_full)
    monkeypatch.setattr(choices, 'maintain_storage', lambda *args, **kwargs: maintenance.append(kwargs))
    worker._flush_measurements()
    assert len(attempts) == 2 and maintenance == [{'required_bytes': 0}]
    assert choices.opportunity(store, oid)
    assert not store.choice_capture.opportunities()


def test_storage_pressure_preserves_capture_context_and_responses_until_due_retry(capture, monkeypatch):
    store, worker = capture
    ledger = store.rec_ledger.evidence()
    reference = choices.freeze_context(store, {'at': 50, 'layers': {'saved': {'version': 1}}})
    from yt_playlist.rec import rec_worker
    for path in ledger.directory.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))
    monotonic = [100]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: monotonic[0])
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    worker.ctx.now_fn = lambda: 10_000
    ledger.byte_budget = sum(path.stat().st_size for path in ledger.directory.glob('*/*/*.json')) + 1
    oid = choices.issue(store, 'albums', [{'id': 'a'}], 100, context=reference)
    assert choices.respond(store, oid, 'choice', 101, option_id='a')
    captures = list(worker.jobs.measurements)
    worker._run_pending()
    assert list(worker.jobs.measurements) == captures
    assert not worker._running and not worker.busy
    assert ledger.get('opportunity', oid) is None
    assert store.choice_capture.get(oid)['at'] == 100
    assert reference['artifact'] in store.choice_capture.artifacts()
    assert ledger.get('artifact', reference['artifact'])
    assert worker.jobs.retry[Job.MEASUREMENT].retry_at == 105
    assert worker.jobs.retry[Job.MEASUREMENT].failures == 1
    assert choices.respond(store, oid, 'open', 102, option_id='a')
    scheduled = []
    monkeypatch.setattr(worker, '_start_loop', lambda: scheduled.append(True))
    worker._retry_measurements()
    assert not scheduled
    # Other recommendation work remains serviceable during capture backoff.
    ratings = []
    monkeypatch.setattr(worker.tasks, 'ratings', lambda: ratings.append(True) or set())
    worker.trigger_rating_update()
    worker._run_pending()
    assert ratings == [True] and not worker._running
    assert ledger.get('opportunity', oid) is None
    scheduled.clear()
    ledger.byte_budget = 100_000
    monotonic[0] = 105
    worker._retry_measurements()
    worker._retry_measurements()
    assert scheduled == [True]
    worker._run_pending()
    assert not worker.jobs.measurements and not worker._running
    assert not store.choice_capture.opportunities() and not store.choice_capture.artifacts()
    assert worker.jobs.retry[Job.MEASUREMENT].failures == 0
    assert ledger.get('opportunity', oid)['at'] == 100
    assert choices.resolve_context(store, {'artifact': ledger.get('opportunity', oid)['context']})['at'] == 50
    assert sorted((row['kind'], row['at']) for row in ledger.records('response')) == [
        ('choice', 101), ('open', 102)]


def test_worker_reclaims_acknowledged_diagnostics_for_the_actual_failed_allocation(capture, monkeypatch):
    from tests.rec.test_choice_bandit import modes
    from yt_playlist.rec.choice_context import checkpoint
    store, worker = capture
    modes(store)
    groups = [{**mode, 'centroid': mode['centroid'].tolist()}
              for mode in store.modes.list_modes(active_only=True)]
    ledger = store.rec_ledger.evidence()
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    worker.ctx.now_fn = lambda: 2000
    old = choices.issue(store, 'mixes', [{'id': 'a', 'mode_id': 1, 'score': 1},
                                       {'id': 'b', 'mode_id': 2, 'score': 0}], 100,
                        context={'at': 100, 'groups': groups, 'layers': {'saved': 'x' * 20_000}})
    choices.respond(store, old, 'choice', 101, option_id='a')
    worker._flush_measurements()
    original = ledger.get('opportunity', old)
    original_responses = ledger.records('response')
    for path in ledger.directory.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))
    choices.maintain_storage(store, 2000)
    learned = checkpoint(ledger, original['context'])
    assert learned and ledger.get('artifact', original['context'])
    # Controlled protected byte pressure keeps this diagnostic graph below the
    # ten-percent ceiling, while leaving too little space for a new publication.
    ledger.put('forecast', 'capacity-fixture', {'padding': 'p' * 400_000})
    used = sum(path.stat().st_size for path in ledger.directory.glob('*/*/*.json'))
    ledger.byte_budget = used + 1
    ordinary = choices.maintain_storage(store, 2000)
    assert ordinary['optional_context_bytes'] > 0 and not ordinary['full']
    assert ledger.get('artifact', original['context'])
    new = choices.issue(store, 'mixes', [{'id': 'c'}], 2000,
                        context={'at': 2000, 'groups': groups, 'layers': {'saved': 'new'}})
    choices.respond(store, new, 'choice', 2001, option_id='c')
    worker._flush_measurements()
    assert not worker.jobs.measurements
    assert ledger.get('opportunity', new)
    assert any(row['opportunity_id'] == new for row in ledger.records('response'))
    assert ledger.get('opportunity', old) == original
    for response in original_responses:
        assert ledger.get('response', response['id']) == response
    assert checkpoint(ledger, original['context']) == learned
    assert ledger.get('artifact', original['context']) is None


@pytest.mark.parametrize('shared_components', [False, True], ids=['manifest', 'shared-components'])
def test_preparation_reclaims_capacity_and_keeps_original_inputs(capture, monkeypatch, shared_components):
    from tests.rec.test_choice_bandit import modes
    from yt_playlist.rec.choice_context import checkpoint

    store, worker = capture
    modes(store)
    groups = [{**mode, 'centroid': mode['centroid'].tolist()}
              for mode in store.modes.list_modes(active_only=True)]
    ledger = store.rec_ledger.evidence()
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    originals = []
    for index in range(3):
        identity = choices.issue(store, 'mixes', [{'id': 'a', 'mode_id': 1, 'score': 1}],
            100 + index, context={'at': 100 + index, 'groups': groups,
                                  'layers': {'saved': str(index) * 80_000}})
        assert choices.respond(store, identity, 'choice', 110 + index, option_id='a')
        originals.append(identity)
    worker._flush_measurements()
    for path in ledger.directory.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))
    choices.maintain_storage(store, 2000)
    offers = [ledger.get('opportunity', identity) for identity in originals]
    learned = {offer['context']: checkpoint(ledger, offer['context']) for offer in offers}
    assert all(learned.values())
    responses = ledger.records('response')
    ledger.put('forecast', 'preparation-capacity-fixture', {'padding': 'p' * 4_000_000})
    ledger.byte_budget = sum(path.stat().st_size for path in ledger.directory.glob('*/*/*.json')) + 1
    ordinary = choices.maintain_storage(store, 2000)
    assert ordinary['optional_context_bytes'] > 200_000
    context = {'at': 2000, 'groups': groups, 'layers': {'saved': 'new preparation' * 100}}
    if shared_components:
        # Reuse an old component, then allocate two new components. Each may
        # need recovery; reclaiming its original owner must not erase the reuse.
        context['layers'] = {'shared': '0' * 80_000, 'first': 'a' * 70_000, 'second': 'b' * 70_000}
    reference = choices.freeze_context(store, context)
    assert choices.resolve_context(store, reference) == context
    for offer in offers:
        assert ledger.get('opportunity', offer['id']) == offer
        assert checkpoint(ledger, offer['context']) == learned[offer['context']]
    assert ledger.records('response') == responses
    used = sum(path.stat().st_size for path in ledger.directory.glob('*/*/*.json'))
    assert int((ledger.directory / '.budget').read_text()) == used <= ledger.byte_budget


def test_failed_preparation_is_not_memoized_as_a_success(capture, monkeypatch):
    from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded

    store, worker = capture
    ledger = store.rec_ledger.evidence()
    ledger.byte_budget = 1
    context = {'at': 100, 'layers': {'saved': 'the original inputs'}}
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        monkeypatch.setattr(reader.rec_ledger, 'evidence', lambda: ledger)
        monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
        with pytest.raises(EvidenceBudgetExceeded):
            reader.memoized_read('preparation-capacity-regression', lambda: choices.freeze_context(reader, context))
    assert store.rec_proposals.get_proposals('album_prepared') is None
    ledger.byte_budget = 10_000
    with store.read_snapshot() as snapshot:
        reference = snapshot.store.memoized_read('preparation-capacity-regression',
            lambda: choices.freeze_context(snapshot.store, context))
    assert choices.resolve_context(store, reference) == context
    assert not worker.jobs.measurements


@pytest.mark.parametrize('phase', ['before-maintenance', 'after-ledger-inventory'])
def test_maintenance_preserves_response_admitted_after_protection_inventory(capture, monkeypatch, phase):
    from yt_playlist.rec import rec_params

    store, worker = capture
    ledger = store.rec_ledger.evidence()
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    now = 100 + (rec_params.get_param(store, 'choice_retention_d') + 1) * 86400
    offer = choices.issue(store, 'albums', [{'id': 'a'}], 100, context={'at': 100, 'groups': []})
    worker._flush_measurements()
    for path in ledger.directory.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))
    maintain = ledger.maintain

    def admit_after_inventory(before, **kwargs):
        assert offer not in kwargs['protected_opportunities']
        def admit():
            assert choices.respond(store, offer, 'choice', now, option_id='a', action_id='late-response')
        if phase == 'before-maintenance':
            admit()
            return maintain(before, **kwargs)
        records = ledger.records

        def after_response_inventory(kind):
            rows = records(kind)
            if kind == 'response':
                admit()
            return rows

        with monkeypatch.context() as patch:
            patch.setattr(ledger, 'records', after_response_inventory)
            return maintain(before, **kwargs)

    monkeypatch.setattr(ledger, 'maintain', admit_after_inventory)
    result = choices.maintain_storage(store, now)
    assert ledger.get('opportunity', offer)
    assert result['retired_unanswered'] == 0
    worker._flush_measurements()
    assert ledger.get('response', 'late-response')['opportunity_id'] == offer
    assert not store.choice_capture.opportunities()


def test_snapshot_maintenance_uses_live_owner_pins(capture, monkeypatch):
    from yt_playlist.rec import rec_params

    store, worker = capture
    ledger = store.rec_ledger.evidence()
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    offer = choices.issue(store, 'albums', [{'id': 'a'}], 100, context={'at': 100, 'groups': []})
    worker._flush_measurements()
    now = 100 + (rec_params.get_param(store, 'choice_retention_d') + 1) * 86400
    with store.read_snapshot() as snapshot:
        assert choices.respond(store, offer, 'choice', now, option_id='a', action_id='snapshot-pending')
        choices.maintain_storage(snapshot.store, now)
    assert ledger.get('opportunity', offer)
    worker._flush_measurements()
    assert ledger.get('response', 'snapshot-pending')


def test_response_after_committed_retirement_is_rejected(capture, monkeypatch):
    from yt_playlist.rec import rec_params

    store, worker = capture
    ledger = store.rec_ledger.evidence()
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    offer = choices.issue(store, 'albums', [{'id': 'a'}], 100, context={'at': 100, 'groups': []})
    worker._flush_measurements()
    now = 100 + (rec_params.get_param(store, 'choice_retention_d') + 1) * 86400
    choices.maintain_storage(store, now)
    assert ledger.get('opportunity', offer) is None
    assert choices.respond(store, offer, 'choice', now, option_id='a') is False
    assert not store.choice_capture.opportunities() and not worker.jobs.measurements


def test_capacity_cleanup_failure_keeps_capture_and_shutdown_releases_it(capture, monkeypatch):
    store, worker = capture
    ledger = store.rec_ledger.evidence()
    ledger.byte_budget = 1
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    def unavailable(*args, **kwargs):
        raise OSError('maintenance unavailable')
    monkeypatch.setattr(choices, 'maintain_storage', unavailable)
    oid = choices.issue(store, 'mixes', [{'id': 'a'}], 100, context={})
    worker._run_pending()
    assert len(worker.jobs.measurements) == 1
    assert store.choice_capture.get(oid) is not None
    assert not worker._running
    worker.shutdown()
    assert not worker.jobs.measurements
    assert store.choice_capture.get(oid) is None


def test_slow_durable_write_does_not_hold_serving_connection(capture, monkeypatch):
    store, worker = capture
    entered, release = threading.Event(), threading.Event()
    put = EvidenceLedger.put

    def slow_put(ledger, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return put(ledger, *args, **kwargs)

    monkeypatch.setattr(EvidenceLedger, 'put', slow_put)
    oid = choices.issue(store, 'radio', [{'id': 'seed'}], 100, context={})
    with ThreadPoolExecutor(2) as threads:
        future = threads.submit(worker._flush_measurements)
        try:
            assert entered.wait(2)
            threads.submit(store.settings.set_setting, 'interactive', 'saved').result(1)
            assert choices.opportunity(store, oid)['options'][0]['id'] == 'seed'
            assert not future.done()
        finally:
            release.set()
        future.result(5)
