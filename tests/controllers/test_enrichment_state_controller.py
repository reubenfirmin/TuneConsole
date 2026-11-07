"""Provider retry and refresh decisions use mocked persistence."""
from contextlib import contextmanager, nullcontext
from sqlite3 import IntegrityError
from types import SimpleNamespace
from unittest.mock import call, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment_state import EnrichmentState, RECONCILE_BATCH
from yt_playlist.controllers.enrichment import Enrichment
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.metadata_deliveries import MetadataDeliveries
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.metadata_delivery_files import MetadataDeliveryFiles
from yt_playlist.core.metadata_runtime_files import RuntimeMetadataFiles
from yt_playlist.core.metadata_observations import MetadataObservation, SubjectChanged
from yt_playlist.core.metadata_contract import Capture, ALTERNATIVE_SECONDS
from yt_playlist.core.original_files import OriginalFiles
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core import native_metadata_policy as native
from yt_playlist.repos.enrichment_state import EnrichmentStateRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def state():
    repo = create_autospec(EnrichmentStateRepo, instance=True, spec_set=True)
    repo.state_transaction.side_effect = nullcontext
    repo.integration_cooldown.return_value = None
    repo.artwork_state.return_value = None
    repo.configuration_inputs.return_value = {'runtime': False, 'last_attempt': 20, 'configuration': {}, 'frontiers': {}}
    repo.provider_refresh_pending.return_value = False
    repo.missing_provider_coverage.return_value = []
    files = create_autospec(RuntimeMetadataFiles, instance=True, spec_set=True)
    deliveries = create_autospec(MetadataDeliveryFiles, instance=True, spec_set=True)
    files.retained.side_effect = deliveries.retained.side_effect = nullcontext
    originals = create_autospec(OriginalFiles, instance=True, spec_set=True)
    originals.retained.side_effect = nullcontext
    return EnrichmentState(repo, create_autospec(MetadataChoices, instance=True, spec_set=True), files, deliveries,
                           create_autospec(MetadataDeliveries, instance=True, spec_set=True),
                           create_autospec(Enrichment, instance=True, spec_set=True), lambda: ['a', 'b'], originals)


@pytest.mark.parametrize(('previous', 'retry_after', 'failures', 'deadline'), [
    (None, 0, 1, 130), (None, 200, 1, 200),
    ({'failures': 1, 'retry_at': 0}, 0, 2, 160),
    ({'failures': 10, 'retry_at': 0}, 0, 11, 3700),
    ({'failures': 1, 'retry_at': 10000}, 0, 2, 10000),
])
def test_integration_outage_backoff_is_bounded_and_preserves_later_deadlines(state, previous, retry_after, failures, deadline):
    state.repository.integration_cooldown.return_value = previous
    result = state.defer_integration('a', now=100, retry_after=retry_after, error='outage')
    assert result == {'provider': 'a', 'failures': failures, 'retry_at': deadline, 'error': 'outage'}
    state.repository.store_integration_cooldown.assert_called_once_with(result)


@pytest.mark.parametrize(('previous', 'status', 'retry_in', 'failures', 'deadline'), [
    (None, 'retry', 0, 1, 160), ({'failures': 1}, 'retry', 0, 2, 220),
    ({'failures': 10}, 'retry', 0, 11, 1000), (None, 'retry', 2000, 1, 2100),
    ({'failures': 10}, 'found', 0, 0, 0), ({'failures': 10}, 'no_match', 0, 0, 0),
])
def test_artwork_backoff_is_separate_and_success_clears_failure_count(state, previous, status, retry_in, failures, deadline):
    state.repository.artwork_state.return_value = previous
    outcome = SimpleNamespace(status=status, retry_in=retry_in, error='detail')
    state.record_artwork_result(7, ['release'], outcome, 100)
    state.repository.store_artwork_result.assert_called_once_with(7, ['release'],
        dict(status=status, failures=failures, retry_at=deadline, error='detail'))


def test_coverage_uses_status_facts_without_loading_provider_documents(state):
    state.repository.coverage_inputs.return_value = [
        dict(provider='disabled', configured='disabled', status='found', pending=True),
        dict(provider='unavailable', configured='missing_key', status='retry', pending=False),
        dict(provider='found', configured='available', status='found', pending=True),
        dict(provider='retry', configured='available', status='retry', pending=True),
        dict(provider='pending', configured='available', status=None, pending=True),
        dict(provider='unknown', configured='available', status=None, pending=False)]
    assert state.provider_coverage(2) == {'disabled': 'disabled', 'unavailable': 'missing_key',
        'found': 'found', 'retry': 'retry', 'pending': 'pending', 'unknown': 'unknown'}
    state.repository.provider_state_inputs.assert_not_called()
    state.repository.state_transaction.assert_not_called()


def test_provider_state_reads_retained_files_after_sql_without_a_reporting_transaction(state, monkeypatch):
    monkeypatch.setattr('yt_playlist.controllers.enrichment_state.clock', lambda now: 1000)
    events = []

    @contextmanager
    def retained():
        events.append('retain')
        yield
        events.append('release')

    state.files.retained.side_effect = retained
    inputs = {'runtime': True, 'subject': {'entry_id': 7, 'recording_id': 8, 'video_id': 'video'},
        'rows': [{'provider_id': 3, 'state': {'recording_id': 8, 'provider': 'a', 'at': 900, 'status': 'found'}}]}
    state.repository.provider_state_inputs.side_effect = lambda *args, **kwargs: events.append('sql') or inputs
    state.files.read.side_effect = lambda *args, **kwargs: events.append('read') or {
        'recording_id': 8, 'entry_id': 99, 'input_video_id': 'other', 'fields': {'bpm': 120, 'year': '2000'}}
    assert state.enrichment_provider_state(8, entry_id=7)['a']['fields'] == {'bpm': 120}
    assert events == ['retain', 'sql', 'read', 'release']
    state.files.read.assert_called_once_with('alternatives', (8, 3), 900., missing=True)
    state.repository.state_transaction.assert_not_called()


@pytest.mark.parametrize('provider_id', [None, 3])
def test_expired_or_unidentified_provider_keeps_completion_without_reading_alternatives(state, monkeypatch, provider_id):
    monkeypatch.setattr('yt_playlist.controllers.enrichment_state.clock', lambda now: 7 * 86400 + 1)
    state.repository.provider_state_inputs.return_value = {'runtime': True, 'subject': None,
        'rows': [{'provider_id': provider_id, 'state': {'recording_id': 8, 'provider': 'a', 'at': 0, 'status': 'found'}}]}
    assert state.enrichment_provider_state(8)['a']['fields'] == {}
    assert state.enrichment_provider_state(8)['a']['status'] == 'found'
    state.files.read.assert_not_called()


def test_attempt_video_filter_uses_the_captured_input_without_reading_it_twice(state, monkeypatch):
    monkeypatch.setattr('yt_playlist.controllers.enrichment_state.clock', lambda now: 1000)
    state.repository.attempt_inputs.return_value = {'runtime': True, 'subject': None,
        'rows': [{'provider_id': 3, 'state': {'recording_id': 8, 'provider': 'a', 'at': 900, 'status': 'found'}}]}
    state.files.read.return_value = {'recording_id': 8, 'input_video_id': 'other', 'fields': {'bpm': 120}}
    assert state.enrichment_attempts('key', 'video') == {}
    state.files.read.assert_called_once()


def test_relational_provider_state_follows_each_fields_delivery_and_reads_shared_bytes_once(state):
    first, second = ('first', 'digest', 5_000_000, 100), ('second', 'digest', 1_000_000, 100)
    rows = [{'field': 'bpm', 'delivery_id': 10, 'reference': first},
            {'field': 'gain', 'delivery_id': 10, 'reference': first},
            {'field': 'genre', 'delivery_id': 11, 'reference': second}]
    state.repository.provider_state_inputs.return_value = {'runtime': False,
        'rows': [{'state': {'provider': 'a', 'status': 'retry'}, 'fields': {'deliveries': rows}}]}
    bodies = {first: {'bpm': 120, 'gain': 2}, second: {'genre': 'Jazz'}}
    state.delivery_files.read.side_effect = lambda ref: {'format': 'enrichment-result-1', 'fields': bodies[ref]}
    assert state.enrichment_provider_state(8)['a']['fields'] == {'bpm': 120, 'gain': 2, 'genre': 'Jazz'}
    assert state.delivery_files.read.call_args_list == [call(first), call(second)]
    state.repository.state_transaction.assert_not_called()


@pytest.mark.parametrize('size', [None, 8 * 1024 * 1024 + 1])
def test_provider_state_rejects_unbounded_delivery_sizes_before_file_reads(state, size):
    state.repository.provider_state_inputs.return_value = {'runtime': False,
        'rows': [{'state': {'provider': 'a'}, 'fields': {'deliveries': [
            {'field': 'bpm', 'delivery_id': 10, 'reference': ('file', 'digest', size, 100)}]}}]}
    with pytest.raises(WorkBudgetExceeded):
        state.enrichment_provider_state(8)
    state.delivery_files.read.assert_not_called()


@pytest.fixture
def attempt_state(state):
    state.repository.attempt_context.return_value = {
        'layout': 'runtime', 'subject': {'entry_id': 7, 'recording_id': 8, 'video_id': 'video'},
        'recording_id': 8, 'cycle': 10, 'previous': None, 'provider_id': 3, 'attempt': None}
    state.repository.capture_inputs.return_value = {**state.repository.attempt_context.return_value['subject'],
        'layout': 'runtime', 'attempts': False}
    state.repository.completion_inputs.return_value = None
    return state


@pytest.mark.parametrize(('previous_cycle', 'previous_failures', 'retry_after', 'failures', 'deadline'), [
    (10, 0, 0, 1, 130), (10, 1, 0, 2, 160), (10, 20, 0, 21, 3700),
    (9, 20, 0, 1, 130), (10, 0, 200, 1, 200),
])
def test_attempt_backoff_resets_by_cycle_caps_delay_and_honors_provider_deadline(
        attempt_state, previous_cycle, previous_failures, retry_after, failures, deadline):
    state = attempt_state
    state.repository.attempt_context.return_value['previous'] = {
        'at': 50, 'cycle': previous_cycle, 'failures': previous_failures}
    state.files.read.return_value = {'fields': {}}
    state.record_enrichment_attempt('key', 'a', {'genre': 'Jazz'}, retry=True, now=100,
        retry_after=retry_after, error='offline', run_id='run')
    result = state.repository.store_attempt.call_args.args[3]
    assert (result['failures'], result['retry_at'], result['status']) == (failures, deadline, 'retry')
    document = state.files.publish.call_args.args[3]
    assert document['error'] == 'offline'
    assert document['expires_at'] == 100 + ALTERNATIVE_SECONDS
    state.repository.completion_inputs.assert_not_called()


@pytest.mark.parametrize(('completed_at', 'captured_at', 'previous_run', 'run', 'now'), [
    (50, 40, 'old', 'new', 100), (50, 60, 'same', 'same', 100), (100, 200, 'old', 'new', 100),
])
def test_older_or_replayed_runtime_attempt_cannot_write_state_or_finish_a_newer_job(
        attempt_state, completed_at, captured_at, previous_run, run, now):
    state = attempt_state
    state.repository.attempt_context.return_value['previous'] = {'at': completed_at, 'cycle': 10, 'failures': 0}
    state.files.read.return_value = {'run_id': previous_run, 'fields': {}}
    captured = Capture(7, 8, 'video', 'a', canonical({'genre': 'Jazz'}), run, captured_at)
    state.record_enrichment_attempt('key', 'a', captured.fields, run_id=run, delivery_id=captured, now=now)
    state.files.publish.assert_not_called()
    state.repository.store_attempt.assert_not_called()
    state.repository.completion_inputs.assert_not_called()


def test_completion_requires_the_whole_captured_result(attempt_state):
    state = attempt_state
    captured = Capture(7, 8, 'video', 'a', canonical({'genre': 'Jazz', 'bpm': 120}), 'run', 10)
    with pytest.raises(ValueError, match='whole captured result'):
        state.record_enrichment_attempt('key', 'a', {'genre': 'Jazz'}, delivery_id=captured, run_id='run', now=20)
    state.files.publish.assert_not_called()
    state.repository.store_attempt.assert_not_called()


def test_attempt_file_failure_cannot_advance_completion_or_clear_refresh_work(attempt_state):
    state = attempt_state
    state.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        state.record_enrichment_attempt('key', 'a', {'genre': 'Jazz'}, now=100)
    state.repository.store_attempt.assert_not_called()
    state.repository.finish_provider_job.assert_not_called()
    state.enrichment.refresh_genre.assert_not_called()


@pytest.mark.parametrize(('prior_entry', 'cycle', 'expected'), [
    (7, 10, {'genre': 'Jazz', 'year': '1999', 'bpm': 120}),
    (99, 10, {'genre': 'Jazz', 'bpm': 120}), (7, 9, {'bpm': 120}),
])
def test_attempt_retains_only_same_cycle_fields_with_proven_item_scope(attempt_state, prior_entry, cycle, expected):
    state = attempt_state
    state.repository.attempt_context.return_value['previous'] = {'at': 50, 'cycle': cycle, 'failures': 1}
    state.files.read.return_value = {'entry_id': prior_entry, 'recording_id': 8, 'input_video_id': 'video',
        'fields': {'genre': 'Jazz', 'year': '1999'}}
    state.record_enrichment_attempt('key', 'a', {'bpm': 120}, now=100)
    assert state.files.publish.call_args.args[3]['fields'] == expected


def test_genre_refresh_and_configuration_reads_follow_the_committed_attempt(attempt_state):
    state, events = attempt_state, []

    @contextmanager
    def transaction():
        events.append('begin attempt')
        yield
        events.append('commit attempt')

    state.repository.state_transaction.side_effect = transaction
    state.repository.completion_inputs.return_value = {'answers': {'b': 'Jazz'}}
    state.provider_order = lambda: events.append('read configuration') or ['a', 'b']
    state.repository.store_attempt.side_effect = lambda *args, **kwargs: events.append('store attempt')
    state.enrichment.refresh_genre.side_effect = lambda *args, **kwargs: events.append('select genre')
    state.repository.finish_provider_job.side_effect = lambda *args: events.append('finish job')
    state.record_enrichment_attempt('key', 'a', {}, now=100)
    assert events == ['begin attempt', 'store attempt', 'commit attempt', 'read configuration', 'select genre', 'finish job']
    state.enrichment.refresh_genre.assert_called_once_with(8, {'a': None, 'b': 'Jazz'}, ['a', 'b'], now=100)


def test_genre_selection_failure_keeps_the_committed_attempt_and_pending_job(attempt_state):
    state = attempt_state
    state.repository.completion_inputs.return_value = {'answers': {}}
    state.enrichment.refresh_genre.side_effect = OSError('interrupted selection')
    with pytest.raises(OSError, match='interrupted selection'):
        state.record_enrichment_attempt('key', 'a', {}, now=100)
    state.repository.store_attempt.assert_called_once()
    state.repository.finish_provider_job.assert_not_called()


@pytest.mark.parametrize('layout', ['inline', 'receipt', 'journal'])
def test_unresolved_attempts_keep_their_installed_capture_format_without_claiming_a_current_subject(state, layout):
    state.repository.attempt_context.return_value = {'layout': 'evidence', 'subject': None}
    state.repository.unresolved_attempt_inputs.return_value = {
        'source_id': 'independent-event', 'layout': layout, 'publication': 'old', 'reference': None}
    state.originals.journal_entries.return_value = {}
    state.originals.publish_journal.return_value = 'new'
    reference = ('path', 'digest', 200, 50)
    state.originals.publish_receipts.return_value = [reference]
    state.record_enrichment_attempt('missing', 'a', {}, now=100)
    call_ = state.repository.store_unresolved_attempt.call_args
    assert call_.args[1] == 'missing'
    assert '"at": 100.0' in call_.args[2]
    assert call_.kwargs == {'publication': 'new' if layout == 'journal' else None,
                           'reference': reference if layout == 'receipt' else None}
    state.repository.store_attempt.assert_not_called()
    state.repository.completion_inputs.assert_not_called()


def test_unresolved_receipt_failure_does_not_install_a_reference(state):
    state.repository.attempt_context.return_value = {'layout': 'evidence', 'subject': None}
    state.repository.unresolved_attempt_inputs.return_value = {
        'source_id': 'event', 'layout': 'receipt', 'publication': None, 'reference': None}
    state.originals.publish_receipts.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        state.record_enrichment_attempt('missing', 'a', {}, now=100)
    state.repository.store_unresolved_attempt.assert_not_called()


@pytest.mark.parametrize('row', [None, {'count': 2, 'error': 'later', 'retry_at': 20}])
def test_retry_report_composes_detached_rows_without_a_writer_or_provider_files(state, row):
    state.repository.retry_status_inputs.return_value = row
    assert state.enrichment_retry_status() == {'count': 2 if row else 0,
        'retry_at': 20 if row else None, 'error': 'later' if row else ''}
    state.repository.state_transaction.assert_not_called()
    state.files.read.assert_not_called()
    state.delivery_files.read.assert_not_called()


def test_relational_result_capture_and_validation_finish_before_the_attempt_unit(attempt_state):
    state, events = attempt_state, []
    state.repository.attempt_context.return_value['layout'] = 'evidence'
    state.repository.capture_inputs.return_value.update(layout='evidence', attempts=True)
    state.choices.capture_result.side_effect = lambda *args, **kwargs: events.append('capture') or (15, {'genre': 16})
    state.choices.result_evidence.side_effect = lambda *args, **kwargs: events.append('validate') or {'genre': 16}

    @contextmanager
    def transaction():
        events.append('begin attempt')
        yield

    state.repository.state_transaction.side_effect = transaction
    state.record_enrichment_attempt('key', 'a', {'genre': 'Jazz'}, now=100, run_id='run')
    assert events == ['capture', 'validate', 'begin attempt']
    assert state.repository.store_attempt.call_args.kwargs['observations'] == {'genre': 16}
    state.choices.result_evidence.assert_called_once_with(7, 'a', 15, {'genre': 'Jazz'}, complete=True, run_id='run')


@pytest.mark.parametrize(('old_status', 'rejected'), [('found', False), ('retry', True)])
def test_relational_attempt_replay_cannot_replace_state_or_change_outcome(attempt_state, old_status, rejected):
    state = attempt_state
    state.repository.attempt_context.return_value.update(layout='evidence', attempt={'id': 10, 'status': old_status})
    state.choices.result_evidence.return_value = {'genre': 16}
    if rejected:
        with pytest.raises(IntegrityError, match='cannot change its outcome'):
            state.record_enrichment_attempt('key', 'a', {'genre': 'Jazz'}, delivery_id=15, now=100)
    else:
        state.record_enrichment_attempt('key', 'a', {'genre': 'Jazz'}, delivery_id=15, now=100)
    state.repository.store_attempt.assert_not_called()
    state.repository.completion_inputs.assert_not_called()


@pytest.fixture
def expiry_state(state):
    state.repository.expiry_state.return_value = {'gc_shard': 4, 'gc_cursor': 20, 'gc_after': 0}
    state.repository.expiry_page.return_value = []
    state.files.collecting.side_effect = lambda: nullcontext(True)
    state.files.expire_alternatives.return_value = 2, 7
    state.files.prune_revisions.return_value = 1
    return state


def test_inactive_runtime_does_not_open_files_or_start_collection(state):
    state.repository.expiry_state.return_value = None
    assert state.expire_provider_alternatives(100) == 0
    state.files.collecting.assert_not_called()
    state.repository.state_transaction.assert_not_called()


def test_an_active_reader_defers_collection_without_taking_a_database_writer(expiry_state):
    state = expiry_state
    state.files.collecting.side_effect = lambda: nullcontext(False)
    assert state.expire_provider_alternatives(100) == 0
    state.files.expire_alternatives.assert_not_called()
    state.repository.state_transaction.assert_not_called()


def test_alternative_collection_keeps_advancing_while_the_recording_scan_is_deferred(expiry_state):
    state = expiry_state
    state.repository.expiry_state.return_value['gc_after'] = 101
    assert state.expire_provider_alternatives(100) == 2
    state.repository.store_expiry_shard.assert_called_once_with(7)
    state.repository.expiry_page.assert_not_called()
    state.repository.store_expiry_cursor.assert_not_called()


def test_an_exhausted_recording_scan_restarts_after_one_hour(expiry_state):
    state = expiry_state
    assert state.expire_provider_alternatives(100) == 2
    state.repository.expiry_page.assert_called_once_with(20, limit=16)
    state.repository.store_expiry_cursor.assert_called_once_with(0, 3700)


def test_expiry_updates_only_candidate_documents_and_keeps_current_native_revisions(expiry_state):
    state, now = expiry_state, ALTERNATIVE_SECONDS + 100
    state.repository.expiry_page.return_value = [{'recording_id': 21,
        'conflicts': [{'id': 12, 'revision': 3, 'updated_at': now}],
        'sources': [{'source_key': 'native-key', 'revision': 5}]}]
    expired = {'provider': 'old', 'value': 'Rock', 'at': 100}
    current = {'provider': 'new', 'value': 'Jazz', 'at': 101}
    state.files.read.return_value = {'candidates': [expired, current]}
    events = []
    state.files.publish.side_effect = lambda *args, **kwargs: events.append('file')
    state.repository.store_conflict_revision.side_effect = lambda *args: events.append('head')
    assert state.expire_provider_alternatives(now) == 4
    state.files.publish.assert_called_once_with('conflicts', (12,), 4, {'candidates': [current]}, current_revision=3)
    assert events == ['file', 'head']
    state.repository.store_conflict_revision.assert_called_once_with(12, 4)
    assert state.files.prune_revisions.call_args_list == [
        call('conflicts', (12,), (3, 4)), call('native', ('native-key',), (5,))]
    state.repository.store_expiry_cursor.assert_called_once_with(21, 0)
    state.repository.store_attempt.assert_not_called()
    state.repository.finish_provider_job.assert_not_called()


def test_failed_expiry_publication_preserves_the_previous_conflict_head(expiry_state):
    state = expiry_state
    state.repository.expiry_page.return_value = [{'recording_id': 21, 'sources': [],
        'conflicts': [{'id': 12, 'revision': 3, 'updated_at': 0}]}]
    state.files.read.return_value = {'candidates': [{'provider': 'old', 'value': 'Rock'}]}
    state.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        state.expire_provider_alternatives(ALTERNATIVE_SECONDS)
    state.repository.store_conflict_revision.assert_not_called()
    state.files.prune_revisions.assert_not_called()
    state.repository.store_expiry_cursor.assert_not_called()


@pytest.fixture
def native_state(state):
    state.repository.capture_inputs.return_value = {
        'entry_id': 7, 'recording_id': 8, 'video_id': 'video', 'native': True, 'layout': 'runtime'}
    state.repository.native_source_inputs.return_value = {'source': None, 'head': None, 'legacy': None}
    return state


def native_observation(value=.2, *, unit='classifier_probability'):
    return MetadataObservation('mood_party', value, 'recording', 'source', 'prediction', unit=unit)


def test_native_result_checks_the_original_subject_before_publishing(native_state):
    state = native_state
    with pytest.raises(SubjectChanged):
        state.record_metadata_observations(7, 'a', [native_observation()], run_id='run', now=10, expected_recording_id=9)
    state.files.publish.assert_not_called()
    state.deliveries.record.assert_not_called()
    state.repository.state_transaction.assert_not_called()


def test_native_result_validates_the_entire_batch_before_any_delivery_or_source(native_state):
    state = native_state
    with pytest.raises(ValueError, match='unit must be bounded text'):
        state.record_metadata_observations(7, 'a', [native_observation(), native_observation(unit=True)], run_id='run', now=10)
    state.files.publish.assert_not_called()
    state.deliveries.record.assert_not_called()
    state.repository.state_transaction.assert_not_called()


def test_failed_native_file_publication_cannot_advance_the_source_or_freshness(native_state):
    state = native_state
    state.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        state.record_metadata_observations(7, 'a', [native_observation()], run_id='run', now=10)
    state.repository.store_native_source.assert_not_called()
    state.deliveries.record.assert_not_called()


@pytest.mark.parametrize(('run', 'at', 'value', 'outcome'), [
    ('newer', 30, .2, 'freshness'), ('older', 5, .7, 'ignored'), ('first', 99, .7, 'changed_replay'),
])
def test_native_result_distinguishes_identical_new_deliveries_older_results_and_changed_replays(native_state, run, at, value, outcome):
    state = native_state
    original = native_observation().payload()
    state.repository.native_source_inputs.return_value = {'source': {'revision': 3}, 'legacy': None,
        'head': {'run_id': 'first', 'accepted_at': 10, 'result_hash': native.result_hash([original])}}
    state.files.read.return_value = {'observations': {'mood_party': native.profile_value(original)}}
    if outcome == 'changed_replay':
        with pytest.raises(ValueError, match='cannot change its result'):
            state.record_metadata_observations(7, 'a', [native_observation(value)], run_id=run, now=at)
    else:
        state.record_metadata_observations(7, 'a', [native_observation(value)], run_id=run, now=at)
    state.files.publish.assert_not_called()
    if outcome == 'freshness':
        update = state.repository.store_native_source.call_args.args[5]
        assert update['revision'] is None
        assert update['head']['accepted_at'] == 30
    else:
        state.repository.store_native_source.assert_not_called()


def test_native_delivery_is_committed_before_its_separate_source_unit(native_state):
    state = native_state
    state.repository.capture_inputs.return_value['layout'] = 'evidence'
    state.repository.native_observation_inputs.return_value = {'existing': False, 'previous': None, 'source': False}
    events = []

    @contextmanager
    def transaction():
        events.append('begin source')
        yield

    state.repository.state_transaction.side_effect = transaction
    state.deliveries.record.side_effect = lambda delivery: events.append('committed delivery') or 15
    state.repository.store_native_observation.side_effect = RuntimeError('interrupted source')
    with pytest.raises(RuntimeError, match='interrupted source'):
        state.record_metadata_observations(7, 'a', [native_observation()], run_id='run', now=10)
    assert events == ['committed delivery', 'begin source']
    state.deliveries.record.assert_called_once()


def test_native_delivery_replay_does_not_replace_a_newer_source_selection(native_state):
    state = native_state
    state.repository.capture_inputs.return_value['layout'] = 'evidence'
    state.repository.native_observation_inputs.return_value = {'existing': True, 'previous': None, 'source': True}
    state.record_metadata_observations(7, 'a', [native_observation()], run_id='old', now=10)
    state.repository.store_native_observation.assert_not_called()
    state.delivery_files.read.assert_not_called()


@pytest.mark.parametrize(('after_id', 'limit'), [(-1, 1), (True, 1), (0, 0), (0, 513), (0, True)])
def test_invalid_observation_page_never_reaches_sql_or_files(state, after_id, limit):
    with pytest.raises(ValueError):
        state.metadata_observations('key', after_id=after_id, limit=limit)
    state.repository.observation_inputs.assert_not_called()
    state.files.retained.assert_not_called()


def test_runtime_observation_pages_resume_inside_a_source_in_sorted_field_order(state):
    state.repository.observation_inputs.return_value = {'layout': 'runtime', 'rows': [
        {'cursor': 2, 'source_key': 'source', 'revision': 3, 'provider': 'a'}]}
    state.files.read.return_value = {'run_id': 'run', 'entry_id': 7, 'recording_id': 8, 'at': 10,
        'observations': {'mood_party': {'value': .2, 'provenance': {}},
                         'bpm': {'value': 120, 'provenance': {}}}}
    page = state.metadata_observations('key', after_id=129, limit=1)
    assert [(row['id'], row['field'], row['value']) for row in page] == [(130, 'mood_party', .2)]
    assert page[0]['delivery_id'] == 'run'
    state.repository.state_transaction.assert_not_called()


@pytest.mark.parametrize(('previous', 'current', 'frontier', 'replace', 'clear', 'restart'), [
    (None, {'fingerprint': 'one', 'status': 'available'}, None, True, False, False),
    ({'fingerprint': 'one', 'status': 'available'}, {'fingerprint': 'two', 'status': 'available'}, None, True, False, True),
    ({'fingerprint': 'one', 'status': 'disabled'}, {'fingerprint': 'one', 'status': 'available'}, None, True, False, True),
    ({'fingerprint': 'one', 'status': 'available'}, {'fingerprint': 'one', 'status': 'disabled'}, {'cursor': 4}, True, True, False),
    ({'fingerprint': 'one', 'status': 'disabled'}, {'fingerprint': 'one', 'status': 'disabled'}, {'cursor': 4}, False, True, False),
])
def test_configuration_change_calculates_only_its_own_refresh_actions(state, previous, current, frontier, replace, clear, restart):
    state.repository.configuration_inputs.return_value.update(configuration={'a': previous} if previous else {}, frontiers={'a': frontier})
    state._configure_providers({'a': current})
    state.repository.store_configuration_changes.assert_called_once_with([
        dict(provider='a', current=current, replace=replace, clear=clear, restart=restart)], 20)


def test_unchanged_configuration_does_not_acquire_a_writer(state):
    current = {'fingerprint': 'one', 'status': 'available'}
    state.repository.configuration_inputs.return_value['configuration'] = {'a': current}
    state._configure_providers({'a': current})
    state.repository.state_transaction.assert_not_called()
    state.repository.store_configuration_changes.assert_not_called()


@pytest.mark.parametrize('runtime', [False, True])
def test_refresh_preserves_newer_retries_and_completed_results_and_keeps_dormant_jobs(state, runtime):
    current = {'fingerprint': 'new', 'status': 'available'}
    frontier = {'cursor': 0, 'cutoff': 1000000 if runtime else 10, 'fingerprint': 'new', 'revision': 1}
    state.repository.provider_refresh_pending.return_value = True
    rows = [
        dict(id=1, first_enriched_at=1, at=1, status='found', last_attempt=10, completed_attempt=10),
        dict(id=2, first_enriched_at=1, at=2, status='retry', last_attempt=11, completed_attempt=10),
        dict(id=3, first_enriched_at=1, at=2, status='found', last_attempt=11, completed_attempt=11),
        dict(id=4, first_enriched_at=None, at=None, status=None, last_attempt=0, completed_attempt=0)]
    state.repository.refresh_inputs.return_value = dict(configuration=current, frontier=frontier, runtime=runtime, rows=rows)
    assert state._advance_provider_refresh('a', current)
    state.repository.store_refresh_page.assert_called_once_with('a', frontier, [1], [1, 2], 4)
    state.repository.refresh_inputs.assert_called_once_with('a', RECONCILE_BATCH)


def test_configuration_replaced_during_refresh_does_not_publish_a_stale_page(state):
    state.repository.provider_refresh_pending.return_value = True
    state.repository.refresh_inputs.return_value = dict(configuration={'fingerprint': 'new', 'status': 'available'},
        frontier={'fingerprint': 'new'}, runtime=False, rows=[])
    assert not state._advance_provider_refresh('a', {'fingerprint': 'old', 'status': 'available'})
    state.repository.store_refresh_page.assert_not_called()
    state.repository.clear_provider_refresh.assert_not_called()


def test_empty_refresh_page_finishes_its_frontier(state):
    current = {'fingerprint': 'new', 'status': 'available'}
    state.repository.provider_refresh_pending.return_value = True
    state.repository.refresh_inputs.return_value = dict(configuration=current, frontier={'fingerprint': 'new'}, runtime=False, rows=[])
    assert not state._advance_provider_refresh('a', current)
    state.repository.clear_provider_refresh.assert_called_once_with('a')


def test_reconciliation_admits_bounded_coverage_batches_and_stops_on_configuration_change(state):
    state.repository.missing_provider_coverage.side_effect = [
        list(range(1, RECONCILE_BATCH + 1)), list(range(RECONCILE_BATCH + 1, 2 * RECONCILE_BATCH + 1))]
    state.repository.admit_provider_coverage.side_effect = [True, False]
    current = {'fingerprint': 'one', 'status': 'available'}
    state.reconcile_provider_configuration({'a': current, 'disabled': {'fingerprint': 'two', 'status': 'disabled'}})
    assert state.repository.admit_provider_coverage.call_args_list == [
        call('a', current, list(range(1, RECONCILE_BATCH + 1))),
        call('a', current, list(range(RECONCILE_BATCH + 1, 2 * RECONCILE_BATCH + 1)))]
    assert state.repository.missing_provider_coverage.call_args_list == [
        call('a', after_id=0, limit=RECONCILE_BATCH), call('a', after_id=RECONCILE_BATCH, limit=RECONCILE_BATCH)]
