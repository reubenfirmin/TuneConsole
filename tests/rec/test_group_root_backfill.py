"""Inventory real predecessor file layouts before opening the retention fence."""
import hashlib
import json

import pytest

from tests.group_history_support import predecessor
from tests.repos.test_compact_group_publication import database
from tests.rec.test_group_roots import groups, context, roots, issue
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.group_preparation_state import ROOTS
from yt_playlist.core.worker_settings import number
from yt_playlist.rec import choice_evidence, group_root_backfill, group_roots
from yt_playlist.rec.choice_context import CORRECTION_CONTRACT


def complete(store):
    backfill = group_root_backfill.Backfill(store)
    for _ in range(50):
        if not backfill.step():
            return
    pytest.fail('Historical group root inventory did not complete')


def historical_offer(store, identity='historical-menu', captured=None):
    # Deliberately reproduce the predecessor: file publication without the new
    # SQL producer hook. Runtime code always uses choice_evidence.issue instead.
    return choice_evidence._issue_record(store, {'id': identity, 'format': 'mixes', 'at': 10,
        'observed_at': 10, 'options': [{'id': 'one', 'score': 1}]}, context() if captured is None else captured)


def test_small_resumable_batches_cover_offers_responses_checkpoints_and_prepared_menus(groups, monkeypatch):
    assert historical_offer(groups) == 'historical-menu'
    ledger = groups.rec_ledger.evidence()
    offer = ledger.get('opportunity', 'historical-menu')
    ledger.put('response', 'historical-response', {'id': 'historical-response', 'opportunity_id': 'historical-menu',
                                                 'kind': 'choice', 'at': 11})
    value = context()
    ledger.put('checkpoint', offer['context'], {'schema': 1, 'contract': CORRECTION_CONTRACT,
        'source_context': offer['context'], 'context': value, 'digest': hashlib.sha256(canonical(value)).hexdigest(),
        'watermark': 'historical-watermark', 'incorporated_at': 12})
    groups.rec_proposals.put_proposals('home_prepared',
        {'choice_context': {'artifact': offer['context']}}, 10)
    monkeypatch.setattr(group_root_backfill, 'BATCH_ROWS', 1)
    backfill = group_root_backfill.Backfill(groups)
    assert backfill.step()
    assert number(groups.conn, ROOTS) == 0
    # A restart discards only the iterator, retaining exact already imported roots.
    complete(groups)
    assert not roots(groups, 'offer')
    assert not roots(groups, 'response')
    assert not roots(groups, 'checkpoint')
    assert not roots(groups, 'prepared')
    assert number(groups.conn, ROOTS) == 1
    assert not groups.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not group_root_backfill.Backfill(groups).step()


def test_missing_historical_context_and_missing_offer_stay_explicitly_unknown(groups):
    ledger = groups.rec_ledger.evidence()
    ledger.put('opportunity', 'missing-context', {'id': 'missing-context', 'context': 'unknown-context-sha',
                                                'at': 10, 'observed_at': 10})
    ledger.put('response', 'missing-offer', {'id': 'missing-offer', 'opportunity_id': 'unknown-offer', 'at': 11})
    complete(groups)
    assert not groups.conn.execute('SELECT 1 FROM group_evidence_contexts '
                                   'WHERE unresolved_context_reason IS NOT NULL').fetchone()
    assert not groups.conn.execute("SELECT 1 FROM retained_group_evidence WHERE reference_kind='resolved_version'").fetchone()
    assert not groups.conn.execute('SELECT 1 FROM group_evidence_contexts WHERE generation_id IS NOT NULL').fetchone()


def test_checkpoint_does_not_recreate_group_roots_after_raw_context_release(groups):
    historical_offer(groups)
    ledger = groups.rec_ledger.evidence()
    offer = ledger.get('opportunity', 'historical-menu')
    value = context()
    ledger.put('checkpoint', offer['context'], {'schema': 1, 'contract': CORRECTION_CONTRACT,
        'source_context': offer['context'], 'context': value, 'digest': hashlib.sha256(canonical(value)).hexdigest(),
        'watermark': 'historical-watermark', 'incorporated_at': 12})
    ledger._path('artifact', offer['context']).unlink()
    complete(groups)
    assert not roots(groups, 'offer')
    assert not roots(groups, 'checkpoint')
    assert not groups.conn.execute('SELECT 1 FROM group_evidence_contexts '
                                   'WHERE unresolved_context_reason IS NOT NULL').fetchone()
    assert groups.conn.execute("SELECT count(*) FROM retained_group_evidence WHERE reference_kind='resolved_version'").fetchone()[0] == 0


def test_malformed_file_never_advances_past_the_unprocessed_owner(groups):
    historical_offer(groups)
    ledger = groups.rec_ledger.evidence()
    path = ledger._path('opportunity', 'historical-menu')
    original = path.read_bytes()
    path.write_bytes(b'{"id":"wrong-source-identity","at":10}')
    backfill = group_root_backfill.Backfill(groups)
    for _ in range(2):
        with pytest.raises(ValueError, match='invalid source identity|required content digest'):
            backfill.step()
        assert number(groups.conn, ROOTS) == 0
        assert backfill.pending_path == path
    path.write_bytes(original)
    for _ in range(10):
        if not backfill.step():
            break
    assert not roots(groups, 'offer')
    assert number(groups.conn, ROOTS) == 1


def test_memory_admission_happens_before_loading_a_historical_record(groups, monkeypatch):
    historical_offer(groups)
    monkeypatch.setattr(group_root_backfill, 'MAX_RECORD_BYTES', 1)
    backfill = group_root_backfill.Backfill(groups)
    with pytest.raises(WorkBudgetExceeded, match='admitted memory'):
        backfill.step()
    assert backfill.pending_path is not None
    assert not roots(groups, 'offer')
    assert number(groups.conn, ROOTS) == 0


def test_cancelled_batch_replays_rolled_back_owners_before_opening_retention(groups, monkeypatch):
    for identity in ('first-owner', 'second-owner', 'third-owner'):
        historical_offer(groups, identity)
    backfill = group_root_backfill.Backfill(groups)
    original = backfill._apply
    visited = []

    def cancel_second(operation):
        original(operation)
        visited.append(operation.identity)
        if len(visited) == 2:
            raise WorkBudgetExceeded('Interrupted after writing an owner')

    monkeypatch.setattr(backfill, '_apply', cancel_second)
    with pytest.raises(WorkBudgetExceeded, match='Interrupted after writing'):
        backfill.step()
    assert not roots(groups, 'offer')
    assert number(groups.conn, ROOTS) == 0
    monkeypatch.setattr(backfill, '_apply', original)
    for _ in range(10):
        if not backfill.step():
            break
    assert not roots(groups, 'offer')
    assert number(groups.conn, ROOTS) == 1
    assert not groups.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_historical_batch_commits_once_for_multiple_owners(groups):
    for identity in ('first-owner', 'second-owner', 'third-owner'):
        historical_offer(groups, identity)
    statements = []
    groups.conn.set_trace_callback(statements.append)
    try:
        assert group_root_backfill.Backfill(groups).step()
    finally:
        groups.conn.set_trace_callback(None)
    assert not roots(groups, 'offer')
    assert sum(statement == 'COMMIT' for statement in statements) == 1


def test_live_producer_between_backfill_batches_is_rooted_and_orphan_reconciliation_is_fenced(groups, monkeypatch):
    historical_offer(groups)
    monkeypatch.setattr(group_root_backfill, 'BATCH_ROWS', 1)
    backfill = group_root_backfill.Backfill(groups)
    assert backfill.step()
    assert issue(groups, 'current-menu') == 'current-menu'
    group_roots.retain(groups, 'offer', 'interrupted-before-file', context(), 10)
    pending_close = group_roots.pending(groups, context(), 10)
    complete(groups)
    assert not roots(groups, 'offer')
    assert len(roots(groups, 'pending')) == 1
    pending_close()
    assert not roots(groups, 'pending')
