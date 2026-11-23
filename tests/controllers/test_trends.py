from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.trends import Trends
from yt_playlist.controllers import trend_preparation
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.repos.trends import TrendsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def trends():
    return Trends(create_autospec(TrendsRepo, instance=True, spec_set=True),
                  create_autospec(RecommendationProposals, instance=True, spec_set=True))


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


def test_rollup_publishes_only_the_output(trends):
    trends.publish_rollup('{"story":null}', 100)
    assert not trends.repository.mock_calls
    trends.proposals.put_encoded.assert_called_once_with('trend_rollups', '{"story":null}', 100)


def test_recap_uses_observed_intensity_when_a_prediction_scores_higher(monkeypatch):
    source = Mock()
    source.trends.ledger_track_plays.return_value = {'mixed': 3, 'pooled': 2}
    measured = {'score': .4, 'support': .8, 'source': 'audio_measurement'}
    pooled = {'score': .9, 'support': .4, 'source': 'artist_genre'}
    profiles = {'mixed': {'vibes': {'intensity_fits': {'energize': measured, 'release': pooled}}},
                'pooled': {'vibes': {'intensity_fits': {'release': pooled}}}}
    month = {'month': '2026-04'}
    personality = Mock()
    monkeypatch.setattr(trend_preparation, 'review', lambda *args, **kwargs: month)
    monkeypatch.setattr(trend_preparation.musical_character, 'load', lambda *_: profiles)
    monkeypatch.setattr(trend_preparation.recap_story, 'personality', personality)
    monkeypatch.setattr(trend_preparation.recap_story, 'build_story', Mock())
    monkeypatch.setattr(trend_preparation.recap_details, 'screens', Mock())

    trend_preparation.assemble(source, 100, [], {}, [month])

    assert personality.call_args.kwargs['audio'] == {'mixed': {'energy': .4}}


@pytest.mark.parametrize('current', [False, True])
def test_pipeline_checks_generation_and_closes_reader_before_ordered_writes(monkeypatch, current):
    snapshot = Mock(spec_set=['is_current', 'release_reader', 'publish_ordered'])
    snapshot.is_current.return_value = current
    writer = Mock(spec_set=['publish_rollup'])
    calculate = Mock(return_value={'story': None})
    monkeypatch.setattr(trend_preparation, 'calculate', calculate)
    def publish(action):
        snapshot.release_reader()
        action()
        return True
    snapshot.publish_ordered.side_effect = publish
    preparation = trend_preparation.TrendPreparation(snapshot=lambda: nullcontext(snapshot),
                                                     publish=writer.publish_rollup)
    order = Mock()
    order.attach_mock(snapshot, 'snapshot')
    order.attach_mock(writer, 'writer')
    assert preparation.build(100) == ({'story': None} if current else None)
    calculate.assert_called_once_with(snapshot, 100)
    if current:
        assert [call[0] for call in order.mock_calls][-4:] == [
            'snapshot.is_current', 'snapshot.publish_ordered', 'snapshot.release_reader', 'writer.publish_rollup']
    else:
        snapshot.release_reader.assert_not_called()
        snapshot.publish_ordered.assert_not_called()
        writer.publish_rollup.assert_not_called()
