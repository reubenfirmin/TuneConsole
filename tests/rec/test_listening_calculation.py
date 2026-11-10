"""Listening arithmetic consumes detached repository results without SQLite."""
from datetime import datetime, timezone
from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yt_playlist.rec import listening_analytics as analytics

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()


def reader_for(samples, *, now=NOW, zone='UTC'):
    repo = Mock(spec=('artist_names', 'catalog_rows', 'mix_playlist_ids', 'video_keys',
                      'player', 'observations', 'plays', 'rating_actions', 'feedback',
                      'prepared', 'impressions', 'prepare_generation'))
    repo.artist_names.return_value = {'1': 'Artist'}
    repo.catalog_rows.return_value = [[{'identity_key': 'a', 'artist_id': 1, 'artist': 'Old name',
                                       'genre': 'Jazz', 'title': 'Song', 'video_id': 'va', 'thumbnail': None}]]
    repo.mix_playlist_ids.return_value = set()
    repo.video_keys.return_value = {'va': 'a'}
    repo.player.return_value = [[{'account_id': 1, 'video_id': 'va', 'playlist_ytm_id': 'LM',
        'kind': kind, 'at': now + at, 'position': position, 'payload': json.dumps(payload)}
        for kind, at, position, payload in samples]]
    for name in ('observations', 'plays', 'rating_actions', 'feedback'):
        getattr(repo, name).return_value = []
    repo.prepared.return_value = {}
    repo.impressions.return_value = {}
    reader = SimpleNamespace(listening_analytics=repo,
        settings=Mock(spec=('get_setting',)), playlists=Mock(spec=('radio_playlist_ids',)))
    reader.settings.get_setting.return_value = zone
    reader.playlists.radio_playlist_ids.return_value = []
    return reader


def test_observed_seconds_exclude_pause_mute_seek_missing_samples_and_duplicate_exit():
    samples = [('tick', 0, 0, {}), ('tick', 30, 30, {}),
        ('state', 40, 40, {'state': 'paused'}), ('state', 60, 40, {'state': 'playing'}),
        ('tick', 90, 70, {}), ('volume', 100, 80, {'volume': 0}),
        ('tick', 130, 110, {}), ('volume', 140, 120, {'volume': 1}),
        ('tick', 170, 150, {}), ('tick', 180, 500, {}),
        ('tick', 300, 620, {}), ('track_exit', 310, 630, {}), ('bye', 310, 630, {})]
    reader = reader_for(samples, now=NOW-1000)
    payload = analytics.calculate(reader, NOW)
    assert sum(row[payload['columns'].index('seconds')] for row in payload['rows']) == 120
    assert sum(row[payload['columns'].index('plays')] for row in payload['rows']) == 0
    assert {row[3] for row in payload['rows']} == {'liked'}
    reader.listening_analytics.player.assert_called_once_with(NOW)


def test_new_start_and_positionless_exit_break_elapsed_intervals():
    samples = [(kind, at, position, {}) for kind, at, position in [
        ('tick', 0, 0), ('tick', 10, 10), ('listen_start', 15, None),
        ('tick', 20, 20), ('tick', 30, 30), ('track_exit', 35, None),
        ('tick', 40, 40), ('tick', 50, 50)]]
    payload = analytics.calculate(reader_for(samples, now=NOW-100), NOW)
    assert sum(row[payload['columns'].index('seconds')] for row in payload['rows']) == 30


def test_local_midnight_and_dst_do_not_borrow_time_from_another_day():
    midnight = datetime(2026, 3, 9, 4, tzinfo=timezone.utc).timestamp()
    reader = reader_for([('tick', -10, 0, {}), ('tick', 20, 30, {})],
                        now=midnight, zone='America/New_York')
    payload = analytics.calculate(reader, midnight+60)
    assert payload['timezone'] == 'America/New_York'
    assert [(row[0], row[payload['columns'].index('seconds')]) for row in payload['rows']] == [
        ('2026-03-08', 10), ('2026-03-09', 20)]


@pytest.mark.parametrize('current', [True, False], ids=['publish', 'stale'])
def test_build_uses_the_captured_repository_plan_and_publishes_only_current_inputs(current):
    reader = reader_for([('tick', 0, 0, {}), ('tick', 30, 30, {})], now=NOW-100)
    plan = object()
    reader.listening_analytics.prepare_generation.return_value = plan

    def guarded_publish(callback):
        if current:
            callback()
        return current

    snapshot = SimpleNamespace(store=reader, _changes=17, publish=guarded_publish)
    publisher = Mock(spec=('publish_generation',))
    store = SimpleNamespace(read_snapshot=lambda: nullcontext(snapshot), listening_analytics=publisher)
    result = analytics.build(store, NOW)
    arguments = reader.listening_analytics.prepare_generation.call_args
    chunks, manifest, edition, built_at = arguments.args
    assert built_at == NOW and arguments.kwargs == {'input_revision': 17}
    assert json.loads(manifest)['timezone'] == 'UTC'
    assert json.loads(manifest)['available'] is True
    assert json.loads(edition)['timezone'] == 'UTC'
    assert len(chunks) == 1
    if current:
        assert sum(row[result['columns'].index('seconds')] for row in result['rows']) == 30
        publisher.publish_generation.assert_called_once_with(plan, 'UTC')
    else:
        assert result is None
        publisher.publish_generation.assert_not_called()
