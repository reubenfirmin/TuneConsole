"""Provider retry and refresh decisions use mocked persistence."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import call, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment_state import EnrichmentState, RECONCILE_BATCH
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
    return EnrichmentState(repo)


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
    state.repository.enrichment_provider_state.assert_not_called()
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
    state.repository.missing_provider_coverage.return_value = list(range(3 * RECONCILE_BATCH))
    state.repository.admit_provider_coverage.side_effect = [True, False]
    current = {'fingerprint': 'one', 'status': 'available'}
    state.reconcile_provider_configuration({'a': current, 'disabled': {'fingerprint': 'two', 'status': 'disabled'}})
    assert state.repository.admit_provider_coverage.call_args_list == [
        call('a', current, list(range(RECONCILE_BATCH))),
        call('a', current, list(range(RECONCILE_BATCH, 2 * RECONCILE_BATCH)))]
    state.repository.missing_provider_coverage.assert_called_once_with('a')
