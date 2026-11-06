"""Selection consumes repository pages and detached taxonomy without a database."""
from types import SimpleNamespace
from unittest.mock import Mock, call

from yt_playlist.rec import stored_groups, stored_home
from yt_playlist.rec.home_selection_policy import CandidateFilter


def reader():
    return SimpleNamespace(
        _read_only=True,
        settings=Mock(get_setting=Mock(return_value=None)),
        recommendations=Mock(),
        rec_model=Mock(muted_artists=Mock(return_value={'muted'}),
                       get_weights=Mock(return_value={}),
                       disliked_key_page=Mock(return_value=[]),
                       suppressed_keys=Mock(return_value=set())),
        rec_query=Mock(track_genres=Mock(return_value={})),
        rec_eligibility=Mock(suppressed_candidate_keys=Mock(return_value={'blocked'})),
        genres=Mock(feature_axes=Mock(side_effect=lambda labels: {
            label: ('jazz', label) for label in labels})))


def track(key, rank, **values):
    return {'key': key, 'pool_rank': rank, 'artist': key, 'album': '',
            'genre': 'Local jazz', 'year': 2000, 'duration': 180,
            'serving_context': {}, **values}


def test_home_load_consumes_all_pages_once_and_resolves_current_eligibility():
    source, ordering = reader(), Mock()
    source.recommendations.selections.side_effect = lambda _surface, lane, **kwargs: (
        [{'id': 7, 'mode_key': 'mode'}] if lane == 'wheelhouse' else [])
    first = [track('blocked', 0), track('muted-track', 1, artist='muted')]
    tail = [track('tail', 2, genre='Old genre')]
    source.recommendations.candidate_page.side_effect = [
        {'tracks': first, 'after': 1, 'exhausted': False},
        {'tracks': tail, 'after': 2, 'exhausted': True}]
    source.rec_query.track_genres.return_value = {'tail': 'Current genre'}
    selected = stored_home.Selection(source, 99, 100, {'genre:Current genre': .2}, ordering)
    assert selected.load('mode', 'wheelhouse') == 7
    assert selected.load('mode', 'wheelhouse') == 7
    assert source.recommendations.candidate_page.call_args_list == [
        call('home', 'mode', 'wheelhouse', generation=99, after=-1, limit=128),
        call('home', 'mode', 'wheelhouse', generation=99, after=1, limit=128)]
    rows = [row for invocation in ordering.add_candidates.call_args_list for row in invocation.args[0]]
    assert [(row[2], row[5], row[8], row[10]) for row in rows] == [
        ('blocked', 'jazz', 1, False), ('muted-track', 'jazz', 1, False), ('tail', 'jazz', 1.2, True)]
    assert tail[0]['genre'] == 'Old genre'
    assert source.genres.feature_axes.call_args_list == [call({'Local jazz'}), call({'Current genre'})]


def test_home_selection_applies_diversity_to_supplied_ranked_rows():
    source, ordering = reader(), Mock()
    source.recommendations.selections.return_value = []
    ordering.has_negative_ordering.return_value = True
    ordering.ranked.return_value = [
        {'key': 'a', 'artist': 'Artist', 'album': 'Shared'},
        {'key': 'same-album', 'artist': 'Other', 'album': 'Shared'},
        {'key': 'b', 'artist': 'Artist', 'album': 'Different'},
        {'key': 'same-artist', 'artist': 'Artist', 'album': ''},
        {'key': 'c', 'artist': 'Last', 'album': ''}]
    selected = stored_home.Selection(source, 99, 100, {}, ordering)
    selected.ppr_counts['mode'] = 128
    filters = CandidateFilter(7, eligible=True)
    result = selected.select('mode', 'ppr', filters, (2, 1), family='jazz')
    assert [row['key'] for row in result] == ['a', 'b', 'c']
    ordering.ranked.assert_called_once_with(filters, mode='mode', ranker='ppr', family='jazz', scale=128)


def test_group_pages_preserve_filters_caps_and_reach_the_later_tail():
    source, ordering = reader(), Mock()
    source.rec_model.get_weights.return_value = {'genre:Banned subtype': 0}
    source.recommendations.candidate_page.side_effect = [
        {'tracks': [track('blocked', 0), track('seen', 1), track('fresh', 2, _radio_fresh=True),
                    track('muted', 3), track('long', 4, duration=100000), track('old', 5, year=1980),
                    track('banned', 6, genre='Banned subtype'), track('good', 7, album='Album')],
         'after': 7, 'exhausted': False},
        {'tracks': [track('same-album', 8, album='Album'), track('tail', 9)],
         'after': 9, 'exhausted': True}]
    selected = stored_groups.Selection(source, 99, 100, ordering)
    result = selected.tracks('version', 'temporal', 1, ('jazz',), (), {'seen'}, 1, 1,
                             {'year_cuts': (1990, 2010)})
    assert [item['key'] for item in result] == ['good', 'tail']
    assert all(item['serving_context']['vibe_affinity'] == .5 for item in result)
    assert source.recommendations.candidate_page.call_args_list == [
        call('home', 'all', 'temporal', generation=99, group_version='version', after=-1, limit=128),
        call('home', 'all', 'temporal', generation=99, group_version='version', after=7, limit=128)]
    ordering.add.assert_not_called()
