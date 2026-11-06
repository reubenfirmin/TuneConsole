from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.trends import Trends
from yt_playlist.rec import trend_rollups
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.trends import TrendsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def trends():
    return Trends(create_autospec(TrendsRepo, instance=True, spec_set=True),
                  create_autospec(RecProposalsRepo, instance=True, spec_set=True))


def test_local_hours_use_each_events_retained_timezone(trends):
    at = datetime(2026, 4, 1, 1, tzinfo=timezone.utc).timestamp()
    trends.repository.play_hour_rows.return_value = [
        {'played_at': at, 'timezone': 'America/Costa_Rica'},
        {'played_at': at, 'timezone': 'Pacific/Kiritimati'},
        {'played_at': at, 'timezone': 'America/Costa_Rica'}]
    hours = trends.play_hours(100, 200, calendar=True)
    assert len(hours) == 24 and sum(hours) == 3
    assert hours[19] == 2 and hours[15] == 1
    trends.repository.play_hour_rows.assert_called_once_with(100, 200, True, calendar=True)


def test_utc_hour_counts_keep_empty_buckets_and_skip_invalid_instants(trends):
    trends.repository.play_hour_rows.return_value = [{'h': 0, 'c': 2}, {'h': 23, 'c': 1}, {'h': None, 'c': 5}]
    hours = trends.play_hours(100, 200, False)
    assert hours == [2] + [0] * 22 + [1]
    trends.repository.play_hour_rows.assert_called_once_with(100, 200, False, calendar=False)


def test_rollup_persists_first_play_before_publishing_output(trends):
    calls = Mock()
    calls.attach_mock(trends.repository, 'repository')
    calls.attach_mock(trends.proposals, 'proposals')
    trends.publish_rollup({'track': [1]}, '{"story":null}', 100, 'import')
    assert [call[0] for call in calls.mock_calls] == ['repository.publish_first_play', 'proposals.put_encoded']
    trends.repository.publish_first_play.assert_called_once_with({'track': [1]}, 'import')
    trends.proposals.put_encoded.assert_called_once_with('trend_rollups', '{"story":null}', 100)


def test_failed_first_play_write_does_not_publish_new_output(trends):
    trends.repository.publish_first_play.side_effect = ValueError('failed write')
    with pytest.raises(ValueError, match='failed write'):
        trends.publish_rollup({}, '{}', 100, None)
    trends.proposals.put_encoded.assert_not_called()


@pytest.mark.parametrize('current', [False, True])
def test_pipeline_checks_generation_and_closes_reader_before_ordered_writes(monkeypatch, current):
    snapshot = Mock(spec_set=['store', 'is_current', 'release_reader'])
    snapshot.store = SimpleNamespace(trends=Mock(spec_set=['prepare_first_play']),
                                     settings=Mock(spec_set=['get_setting']))
    snapshot.is_current.return_value = current
    snapshot.store.trends.prepare_first_play.return_value = {'track': [1]}
    snapshot.store.settings.get_setting.return_value = None
    writer = Mock(spec_set=['publish_rollup'])
    store = SimpleNamespace(read_snapshot=lambda: nullcontext(snapshot), trends=writer)
    monkeypatch.setattr(trend_rollups, '_calculate', Mock(return_value={'story': None}))
    order = Mock()
    order.attach_mock(snapshot, 'snapshot')
    order.attach_mock(writer, 'writer')
    assert trend_rollups.build(store, 100) == ({'story': None} if current else None)
    if current:
        assert [call[0] for call in order.mock_calls][-3:] == [
            'snapshot.is_current', 'snapshot.release_reader', 'writer.publish_rollup']
    else:
        snapshot.release_reader.assert_not_called()
        writer.publish_rollup.assert_not_called()
