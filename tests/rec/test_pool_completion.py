"""A completed job must be checked for output before its card stops polling."""
from types import SimpleNamespace

import pytest

from yt_playlist.rec.home_cards import HomeCards


@pytest.mark.parametrize('kind,lane', [('fresh_pool', 'fresh'), ('library_pool', 'explore')])
def test_completion_between_selection_and_view_keeps_polling(kind, lane):
    class CompletingJob:
        parameters = {'lane': lane, 'epoch': 0, 'include_genres': [], 'avoid_genres': []}
        error = None
        events = [{'text': 'Preparing your mix…'}]
        reads = 0

        @property
        def done(self):
            self.reads += 1
            return self.reads > 1

    job = CompletingJob()
    job.kind = kind
    cards = HomeCards(None, None, None, genre_options=[])
    result = cards.pool(job, 1, '/home/pool/1')
    assert result.proto is None
    assert result.pool['building']
    assert result.pool['message'] == 'Preparing your mix…'


@pytest.mark.parametrize('kind,lane', [('fresh_pool', 'fresh'), ('library_pool', 'explore')])
def test_finished_empty_job_has_terminal_empty_state(kind, lane, monkeypatch):
    job = SimpleNamespace(kind=kind, done=True, error=None, events=[], exclude_keys=[],
        parameters={'lane': lane, 'epoch': 0, 'include_genres': [], 'avoid_genres': []})
    cards = HomeCards(None, None, None, genre_options=[])
    checked = []
    monkeypatch.setattr(cards, 'built_fresh', lambda *args: checked.append('fresh'))
    monkeypatch.setattr(cards, 'built_library', lambda *args: checked.append('library'))
    result = cards.pool(job, 1, '/home/pool/1')
    assert checked == ['fresh' if lane == 'fresh' else 'library']
    assert not result.pool['building']
    assert 'Not enough matching songs' in result.pool['message']


@pytest.mark.parametrize('kind,lane', [('fresh_pool', 'fresh'), ('library_pool', 'explore')])
def test_prepared_menu_keeps_completed_job_poll_until_current_output_is_checked(kind, lane):
    job = SimpleNamespace(kind=kind, done=True, error=None, events=[{'text': 'Preparing your mix…'}],
        parameters={'lane': lane, 'epoch': 0, 'include_genres': [], 'avoid_genres': []})
    cards = HomeCards(None, None, None, genre_options=[])
    view = cards.pool_view(job.parameters, job)
    assert view['building']
    assert 'Not enough' not in view['message']
    job.error = 'Provider could not find songs. Try again.'
    failed = cards.pool_view(job.parameters, job)
    expected = 'Fresh mix' if lane == 'fresh' else 'Mix'
    assert not failed['building'] and failed['message'] == expected + ' not completed. You can try again.'
    assert job.error not in failed['message']
