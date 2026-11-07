from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.home_selection import HomeSelection
from yt_playlist.rec.home_selection_policy import CandidateFilter
from yt_playlist.repos.home_selection import HomeSelectionRepo

pytestmark = pytest.mark.usefixtures('no_database')


def candidate(key, family):
    return (7, 0, key, 'Artist', '', family, bool(family), 2000, 1, 0, 1)


def test_distances_are_prepared_outside_sql_and_include_later_candidate_pages():
    repository = create_autospec(HomeSelectionRepo, instance=True, spec_set=True)
    repository.missing_distance_families.side_effect = [['jazz', 'unlisted-family'], [], [''], []]
    selection = HomeSelection(repository)
    selection.add_candidates([candidate('a', 'jazz'), candidate('b', 'unlisted-family')])
    filters = CandidateFilter(7, near_family='jazz')
    selection.count(filters)
    repository.add_distances.assert_called_once_with([
        ('jazz', 'jazz', 0.), ('jazz', 'unlisted-family', 1.)])
    selection.count(filters)
    assert repository.add_distances.call_count == 1
    selection.add_candidates([candidate('c', None)])
    selection.count(filters)
    assert repository.add_distances.call_args.args[0] == [('jazz', '', 1.)]


def test_distance_cache_is_not_advanced_after_a_failed_write():
    repository = create_autospec(HomeSelectionRepo, instance=True, spec_set=True)
    repository.missing_distance_families.side_effect = [['jazz'], ['jazz'], []]
    selection = HomeSelection(repository)
    selection.add_candidates([candidate('a', 'jazz')])
    repository.add_distances.side_effect = RuntimeError('interrupted')
    filters = CandidateFilter(7, near_family='jazz')
    with pytest.raises(RuntimeError, match='interrupted'):
        selection.count(filters)
    repository.count.assert_not_called()
    repository.add_distances.side_effect = None
    selection.count(filters)
    assert repository.add_distances.call_count == 2


def test_unknown_ranker_is_rejected_before_query_execution():
    repository = create_autospec(HomeSelectionRepo, instance=True, spec_set=True)
    with pytest.raises(ValueError, match='Unknown Home ordering'):
        HomeSelection(repository).ranked(CandidateFilter(7), ranker='unexpected')
    repository.ranked.assert_not_called()
