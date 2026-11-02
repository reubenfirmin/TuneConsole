"""Exercise the actual queue, proposal and file-ledger group ownership paths."""
import json

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_group_retention import enable, drain
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.store import Store
from yt_playlist.core import group_documents, group_evidence, policy_documents
from yt_playlist.rec import choice_evidence, group_roots
from yt_playlist.rec.choice_capture import CaptureQueue
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo


@pytest.fixture
def groups(database):
    store, _ = database
    store.taste_groups = CompactTasteGroupsRepo(store)
    return store


def context(generation='first', prefix='first'):
    return {'at': 10, 'group_generation': generation,
            'musical_groups': [{'group_id': 'leaf', 'version': prefix + '-leaf',
                                'parent_version': prefix + '-parent'}]}


def issue(store, identity='menu'):
    return choice_evidence.issue(store, 'mixes', [{'id': 'one', 'score': .7}, {'id': 'two', 'score': .3}],
                                 10, context=context(), opportunity_id=identity, reuse=True)


def roots(store, kind):
    return [row[0] for row in store.conn.execute('SELECT external_key FROM group_evidence_owners '
                                                'WHERE kind=? ORDER BY external_key', (kind,))]


def test_queued_choices_do_not_pin_group_history_but_keep_durable_events(groups):
    jobs = []
    groups.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    enable(groups)
    assert issue(groups) == 'menu'
    assert not roots(groups, 'pending')
    assert not groups.rec_ledger.evidence().get('opportunity', 'menu')
    assert choice_evidence.respond(groups, 'menu', 'choice', 11, option_id='one')
    assert not roots(groups, 'pending')
    drain(groups)
    assert groups.taste_groups.definition('first-leaf', generation='first') is None
    assert jobs[0].run() == 'menu'
    jobs[0].close()
    assert not roots(groups, 'offer') and not roots(groups, 'pending')
    assert jobs[1].run()
    jobs[1].close()
    assert not roots(groups, 'pending')
    assert not roots(groups, 'response')
    assert groups.rec_ledger.evidence().get('response', 'menu:choice:one')['option_id'] == 'one'


def test_deduplication_and_cancellation_release_only_their_own_pending_capture(groups):
    jobs = []
    groups.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    assert issue(groups) == 'menu'
    assert issue(groups) == 'menu'
    assert len(jobs) == 1 and not roots(groups, 'pending')
    jobs[0].close()
    jobs[0].close()
    assert not roots(groups, 'pending')
    assert not roots(groups, 'offer')
    groups.choice_capture = CaptureQueue(lambda job: False)
    assert issue(groups, 'rejected-menu') is None
    assert not roots(groups, 'pending')


def test_pending_retirement_uses_the_os_lifetime_instead_of_age(groups):
    close = group_roots.pending(groups, context(), -1000000)
    identity = roots(groups, 'pending')[0]
    path = group_documents.directory(groups.conn) / '.pending' / (identity + '.lock')
    group_roots.reap_pending(groups)
    assert roots(groups, 'pending') == [identity] and path.exists()
    close()
    assert not roots(groups, 'pending') and not path.exists()
    # Model a process which committed its pending root and then died. Releasing
    # its OS lock is the only death evidence; no timestamp or PID guess is used.
    with path.open('x+b') as stream:
        unlock = policy_documents._try_file_lock(stream, exclusive=True)
        with group_roots.writer(groups) as conn:
            group_evidence.retain(conn, 'pending', identity, context(), 1000000)
        group_roots.reap_pending(groups)
        assert roots(groups, 'pending') == [identity]
        unlock()
    group_roots.reap_pending(groups)
    assert not roots(groups, 'pending') and not path.exists()


def test_database_close_leaves_contended_retirement_for_the_lifetime_reaper(groups):
    close = group_roots.pending(groups, context(), 10)
    identity = roots(groups, 'pending')[0]
    path = group_documents.directory(groups.conn) / '.pending' / (identity + '.lock')
    other = Store(groups._db_path)
    other._bind_repositories()
    try:
        with group_roots.admission(other):
            groups.conn.close()
            assert roots(other, 'pending') == [identity] and path.exists()
        close()  # A later finalizer cannot use the closed connection.
        group_roots.reap_pending(other)
        assert not roots(other, 'pending') and not path.exists()
    finally:
        other.conn.close()


def test_irrelevant_historical_generation_is_not_a_choice_dependency(groups):
    jobs = []
    groups.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    assert choice_evidence.issue(groups, 'mixes', [{'id': 'one', 'genres':['House']}], 10,
        context=context('second', 'first'), opportunity_id='wrong-generation') == 'wrong-generation'
    assert len(jobs) == 1 and not roots(groups, 'pending') and not roots(groups, 'offer')
    assert jobs[0].run() == 'wrong-generation'
    jobs[0].close()


def test_proposal_replacement_and_sql_deletion_release_exact_prepared_owners(groups):
    first = {'context': context()}
    second = {'context': context('second', 'second')}
    groups.rec_proposals.put_proposals('home_prepared', first, 10)
    assert roots(groups, 'prepared') == ['proposal:home_prepared:context']
    with pytest.raises(RuntimeError, match='controlled rollback'), groups.conn:
        groups.conn.execute('BEGIN IMMEDIATE')
        groups.rec_proposals.publish_prepared_home(json.dumps(second), 11)
        raise RuntimeError('controlled rollback')
    assert groups.rec_proposals.get_proposals('home_prepared') == first
    assert groups.conn.execute("SELECT generation_id FROM group_evidence_generations WHERE kind='prepared'").fetchone()[0] == 1
    assert issue(groups) == 'menu'
    groups.rec_proposals.put_proposals('home_prepared', second, 12)
    with groups.conn:
        groups.conn.execute("DELETE FROM rec_proposals WHERE surface='home_prepared'")
    assert not roots(groups, 'prepared') and not roots(groups, 'offer')
    enable(groups)
    drain(groups)
    assert groups.taste_groups.definition('first-leaf') is None


def test_comparison_envelope_retains_context_without_decoding_its_payload(groups):
    envelope = {'codec': 'invalid-compressed-data-is-never-read-for-roots', 'data': 'opaque',
                'context': context(), 'run_id': 'trial'}
    groups.rec_proposals.put_proposals('model_pair:trial:incumbent:mixes', envelope, 10)
    assert roots(groups, 'comparison') == ['proposal:model_pair:trial:incumbent:mixes:context']
    with groups.conn:
        groups.conn.execute("DELETE FROM rec_proposals WHERE surface LIKE 'model_pair:%'")
    assert not roots(groups, 'comparison')


def test_context_roots_do_not_expand_unrelated_model_components(groups, monkeypatch):
    ledger = groups.rec_ledger.evidence()
    versions = ledger.artifact(context()['musical_groups'])
    policy = ledger.artifact({'large_policy': ['large-policy-values']})
    matrices = ledger.artifact({'large_matrix': [[1, 2, 3]]})
    captured = ledger.artifact({'group_generation': 'first', 'musical_groups': {'$artifact': versions},
                                 'policy': {'$artifact': policy}, 'layers': {'$artifact': matrices}})
    original = ledger.get

    def get(kind, identity):
        assert identity not in (policy, matrices), 'Group roots expanded an unrelated model component'
        return original(kind, identity)
    monkeypatch.setattr(ledger, 'get', get)
    monkeypatch.setattr(groups.rec_ledger, 'evidence', lambda: ledger)
    assert group_roots.retain(groups, 'prepared', 'partial-read', {'artifact': captured}, 10)
    assert roots(groups, 'prepared') == ['partial-read']


def test_evidence_publication_lock_is_reentrant_locally_and_fences_another_owner(groups):
    other = Store(groups._db_path)
    other._bind_repositories()
    try:
        with group_roots.admission(groups), group_roots.admission(groups):
            with pytest.raises(WorkBudgetExceeded, match='another process'):
                with group_roots.admission(other):
                    pytest.fail('Another owner acquired the evidence admission lock')
        with group_roots.admission(other):
            assert group_roots.retain(other, 'pending', 'other-owner', context(), 10)
    finally:
        other.conn.close()


def test_actual_ledger_retirement_releases_an_unanswered_offer_then_allows_history_collection(groups):
    assert issue(groups) == 'menu'
    ledger = groups.rec_ledger.evidence()
    with group_roots.admission(groups):
        result = ledger.maintain(100, orphan_before=100,
            retire_references=lambda offers, checkpoints: group_roots.retired(groups, offers, checkpoints))
    assert result['retired_unanswered'] == 1
    assert not ledger.get('opportunity', 'menu') and not roots(groups, 'offer')
    enable(groups)
    drain(groups)
    assert groups.taste_groups.definition('first-leaf') is None


def test_response_survives_collection_without_a_group_reference(groups):
    assert issue(groups) == 'menu'
    assert choice_evidence.respond(groups, 'menu', 'choice', 11, option_id='one')
    group_roots.release(groups, 'offer', 'menu')
    enable(groups)
    drain(groups)
    assert not roots(groups, 'response')
    assert groups.taste_groups.definition('first-leaf') is None
    assert groups.rec_ledger.evidence().get('response', 'menu:choice:one') is not None


def test_failed_choice_file_does_not_keep_unrelated_group_history(groups, monkeypatch):
    ledger = groups.rec_ledger.evidence()
    monkeypatch.setattr(groups.rec_ledger, 'evidence', lambda: ledger)

    def fail(*args, **kwargs):
        raise OSError('controlled file publication failure')
    monkeypatch.setattr(ledger, 'put', fail)
    with pytest.raises(OSError, match='controlled file'):
        issue(groups)
    assert not roots(groups, 'offer')
    assert not ledger.get('opportunity', 'menu')
    enable(groups)
    drain(groups)
    assert groups.taste_groups.definition('first-leaf') is None
