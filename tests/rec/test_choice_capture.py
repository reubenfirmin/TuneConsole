"""Listening capture must leave serving responsive and preserve the actual offered state."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity
from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence as choices, choice_outcomes
from yt_playlist.rec.rec_worker import RecWorker


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
