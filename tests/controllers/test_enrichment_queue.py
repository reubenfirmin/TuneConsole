from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment_queue import EnrichmentQueue
from yt_playlist.enrich import queue_policy
from yt_playlist.repos.enrichment_queue import EnrichmentQueueRepo
from yt_playlist.repos.enrichment_state import EnrichmentStateRepo
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')


def queue(*, read_only=True):
    repository = create_autospec(EnrichmentQueueRepo, instance=True, spec_set=True)
    state = create_autospec(EnrichmentStateRepo, instance=True, spec_set=True)
    settings = create_autospec(SettingsRepo, instance=True, spec_set=True)
    settings.get_setting.return_value = '100'
    return EnrichmentQueue(repository, state, settings, read_only=read_only,
        read_snapshot=Mock(), memoized_read=Mock(side_effect=lambda _, calculate: calculate()))


def candidate(identity, *, completed=None):
    return {'recording_id': identity, 'source': 'library', 'id': identity,
            'identity_key': f'song-{identity}', 'video_id': f'v{identity}', 'title': 'Song',
            'artist': 'Artist', 'last_enriched_at': completed, 'first_enriched_at': completed,
            'thumbnail': None, 'mb_recording_id': None}


def test_visible_work_is_bounded_and_only_refreshes_include_provider_jobs():
    service = queue()
    service.repository.priority_candidates.return_value = [candidate(1), candidate(2, completed=10)]
    service.state.enrichment_provider_jobs.return_value = ['deezer']
    result = service.priority_enrich_batch([*range(120), 0], 24, 500, providers=['deezer'], now=1000)
    selected = service.repository.priority_candidates.call_args
    assert selected.args == (list(range(96)), 24, 500)
    assert selected.kwargs['providers'] == ['deezer']
    assert selected.kwargs['now'] == 1000
    assert result[0]['job_id'] == 1 and 'provider_jobs' not in result[0]
    assert result[1]['provider_jobs'] == ['deezer']
    service.state.enrichment_provider_jobs.assert_called_once_with(2)


@pytest.mark.parametrize('limit,providers', [(0, None), (40, [])])
def test_empty_selection_skips_ranking(limit, providers):
    service = queue()
    assert service.next_enrich_batch(limit, providers=providers, now=1000) == []
    service.repository.has_candidates.assert_not_called()
    service.memoized_read.assert_not_called()


def test_fully_deferred_queue_skips_priority_calculation():
    service = queue()
    service.repository.has_candidates.return_value = False
    assert service.next_enrich_batch(40, providers=['deezer'], now=1000) == []
    service.repository.priority_order.assert_not_called()
    service.memoized_read.assert_not_called()


def test_ranking_receives_caught_up_boundary_and_respects_pagination():
    service = queue()
    service.repository.has_candidates.return_value = True
    service.repository.priority_order.return_value = '[8,2,5]'
    service.repository.select_candidates.return_value = [candidate(2)]
    assert service.next_enrich_batch(1, 1, now=1000)[0]['recording_id'] == 2
    service.repository.priority_order.assert_called_once_with(caught_up=100., ordering=queue_policy.PRIORITY_ORDER)
    selection = service.repository.select_candidates.call_args
    assert selection.args == (1, 1)
    assert selection.kwargs['priorities'] == '[8,2,5]'
    service.read_snapshot.assert_not_called()
    service.repository.admit_candidates.assert_not_called()


@pytest.mark.parametrize('resweep', [False, True])
def test_live_admission_happens_after_reader_closes_in_bounded_batches(resweep):
    service, reader = queue(read_only=False), queue()
    candidates, events = [candidate(i) for i in range(130)], []
    reader.repository.provider_configuration.return_value = [('deezer', 'original', 'available')]
    reader.repository.has_candidates.return_value = True
    reader.repository.select_candidates.return_value = candidates
    reader.repository.resweep_candidates.return_value = candidates

    @contextmanager
    def snapshot():
        events.append('opened')
        try:
            yield SimpleNamespace(store=SimpleNamespace(enrichment_queue=reader))
        finally:
            events.append('closed')

    def admit(rows, configuration, **criteria):
        assert events[0:2] == ['opened', 'closed']
        assert len(rows) <= 64
        assert configuration == [('deezer', 'original', 'available')]
        assert criteria['now'] == 1000
        assert criteria['stale_before'] == (500 if resweep else None)
        events.append('admitted')
        return [row for row in rows if row['recording_id'] % 2 == 0]

    service.read_snapshot.side_effect = snapshot
    service.repository.admit_candidates.side_effect = admit
    result = (service.resweep_batch(130, 500, providers=['deezer'], now=1000) if resweep
              else service.next_enrich_batch(130, providers=['deezer'], now=1000))
    assert [track['recording_id'] for track in result] == list(range(0, 130, 2))
    assert events == ['opened', 'closed', 'admitted', 'admitted', 'admitted']
