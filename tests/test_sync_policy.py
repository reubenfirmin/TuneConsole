"""Retry and retained-result policy uses supplied state and repository contracts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from yt_playlist.library import sync_outcomes as policy


def test_retry_policy_retains_nested_success_and_caps_backoff_without_a_database():
    old = {'accounts': {'1': {'status': 'complete', 'last_success_at': 100,
        'resources': {'playlists': {'status': 'complete', 'items': {
            'healthy': {'status': 'complete', 'last_attempt_at': 100, 'last_success_at': 100, 'failures': 0},
            'retry': {'status': 'failed', 'last_attempt_at': 150, 'last_success_at': 100,
                      'failures': 16, 'retry_at': 3750}}}}}}}
    result = {'status': 'partial', 'accounts': {'1': {'status': 'partial', 'resources': {
        'playlists': {'status': 'partial', 'items': {
            'healthy': policy.retained(old['accounts']['1']['resources']['playlists']['items']['healthy']),
            'retry': {'status': 'failed'}}}, 'history': {'status': 'complete'}}}}, 'resources': {}}
    stamped = policy.stamp_result(result, deepcopy(old), 200)
    account = stamped['accounts']['1']
    items = account['resources']['playlists']['items']
    assert items['healthy'] == old['accounts']['1']['resources']['playlists']['items']['healthy']
    assert items['retry'] == {'status': 'failed', 'last_attempt_at': 200,
                              'last_success_at': 100, 'failures': 16, 'retry_at': 3800}
    assert account['last_success_at'] == 100 and account['last_attempt_at'] == 200
    assert account['resources']['history'] == {'status': 'complete', 'last_attempt_at': 200,
                                               'last_success_at': 200, 'failures': 0}
    assert '_retained' not in str(stamped)


def test_schedule_uses_repository_state_and_respects_retry_and_freshness():
    repo = Mock(spec=['state', 'begin_attempt'])
    repo.state.return_value = {'accounts': {'7': {'resources': {
        'history': {'status': 'failed', 'retry_at': 300},
        'playlists': {'status': 'complete', 'last_success_at': 100}}}}}
    store = SimpleNamespace(sync=repo)
    schedule = policy.SyncSchedule(store, 200, automatic=True, max_age_s=150)
    assert not schedule.resource_due(7, 'history')
    assert not schedule.resource_due(7, 'playlists')
    assert schedule.resource_due(7, 'missing')
    assert policy.SyncSchedule(store, 300, automatic=True).resource_due(7, 'history')
    assert policy.SyncSchedule(store, 250, automatic=True, max_age_s=150).resource_due(7, 'playlists')
    assert policy.SyncSchedule(store, 200, automatic=True, retry_now=True).resource_due(7, 'history')
    repo.begin_attempt.return_value = 18
    assert policy.begin_attempt(store, 200) == 18
    repo.begin_attempt.assert_called_once_with(200)
