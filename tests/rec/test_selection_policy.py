"""Selection consumes a ranked pool and detached taxonomy without a database."""
from types import SimpleNamespace
from unittest.mock import Mock

from yt_playlist.rec import stored_groups, stored_home
from yt_playlist.rec.home_selection_policy import CandidateFilter


def reader():
    return SimpleNamespace(
        _read_only=True,
        settings=Mock(get_setting=Mock(return_value=None)),
        recommendations=Mock(),
        artists=SimpleNamespace(credited_rows=lambda rows: rows),
        rec_model=Mock(muted_artists=Mock(return_value={'muted'}),
                       get_weights=Mock(return_value={}),
                       suppressed_keys=Mock(return_value={'blocked'})),
        rec_query=Mock(track_genres=Mock(return_value={})),
        genres=Mock(feature_axes=Mock(side_effect=lambda labels: {
            label: ('jazz', label) for label in labels})))


def track(key, rank, **values):
    return {'key': key, 'pool_rank': rank, 'artist': key, 'album': '',
            'genre': 'Local jazz', 'year': 2000, 'duration': 180,
            'serving_context': {}, **values}


def test_home_load_consumes_complete_pool_once_and_resolves_current_eligibility():
    source, ordering = reader(), Mock()
    source.recommendations.selections.side_effect = lambda _surface, lane, **kwargs: (
        [{'id': 7, 'mode_key': 'mode'}] if lane == 'wheelhouse' else [])
    first = [track('blocked', 0), track('muted-track', 1, artist='muted')]
    tail = [track('tail', 2, genre='Old genre')]
    pool = ('home', 99, 'mode', 'wheelhouse')
    source.recommendations.complete_candidates.return_value = {pool: first + tail}, None
    source.rec_query.track_genres.return_value = {'tail': 'Current genre'}
    selected = stored_home.Selection(source, 99, 100, {'genre:Current genre': .2}, ordering)
    assert selected.load('mode', 'wheelhouse') == 7
    assert selected.load('mode', 'wheelhouse') == 7
    source.recommendations.complete_candidates.assert_called_once_with([pool])
    source.recommendations.candidate_page.assert_not_called()
    ordering.add_candidates.assert_called_once()
    rows = ordering.add_candidates.call_args.args[0]
    assert [(row[2], row[5], row[8], row[9]) for row in rows] == [
        ('blocked', 'jazz', 1, False), ('muted-track', 'jazz', 1, False), ('tail', 'jazz', 1.2, True)]
    assert tail[0]['genre'] == 'Old genre'
    source.genres.feature_axes.assert_called_once_with({'Local jazz', 'Current genre'})


def test_home_selection_applies_diversity_to_supplied_ranked_rows():
    source, ordering = reader(), Mock()
    source.recommendations.selections.return_value = []
    ordering.ranked.return_value = [
        {'key': 'a', 'artist': 'Artist', 'album': 'Shared'},
        {'key': 'same-album', 'artist': 'Other', 'album': 'Shared'},
        {'key': 'b', 'artist': 'Artist', 'album': 'Different'},
        {'key': 'same-artist', 'artist': 'Artist', 'album': ''},
        {'key': 'c', 'artist': 'Last', 'album': ''}]
    selected = stored_home.Selection(source, 99, 100, {}, ordering)
    filters = CandidateFilter(7, eligible=True)
    result = selected.select('mode', 'ppr', filters, (2, 1), family='jazz')
    assert [row['key'] for row in result] == ['a', 'b', 'c', 'same-album', 'same-artist']
    ordering.ranked.assert_called_once_with(filters, mode='mode', ranker='ppr', family='jazz')


def test_group_pool_preserves_filters_caps_and_reaches_the_later_tail():
    source, ordering = reader(), Mock()
    source.rec_model.get_weights.return_value = {'genre:Banned subtype': 0}
    pool = ('home', 99, 'all', 'temporal')
    tracks = [track('blocked', 0), track('seen', 1), track('fresh', 2, _radio_fresh=True),
              track('muted', 3), track('long', 4, duration=100000), track('old', 5, year=1980),
              track('banned', 6, genre='Banned subtype'), track('good', 7, album='Album'),
              track('same-album', 8, album='Album'), track('tail', 9)]
    source.recommendations.complete_candidates.return_value = {pool: tracks}, None
    selected = stored_groups.Selection(source, 99, 100, ordering)
    result = selected.tracks('version', 'temporal', 1, ('jazz',), (), {'seen'}, 1, 1,
                             {'year_cuts': (1990, 2010)})
    assert [item['key'] for item in result] == ['good', 'tail', 'same-album']
    assert all(item['serving_context']['vibe_affinity'] == .5 for item in result)
    source.recommendations.complete_candidates.assert_called_once_with([pool], group_version='version')
    source.recommendations.candidate_page.assert_not_called()
    ordering.add.assert_not_called()
