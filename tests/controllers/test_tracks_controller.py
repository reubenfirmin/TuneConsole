"""Recording matching and rating decisions use supplied repository facts."""
from contextlib import contextmanager
from math import inf, nextafter
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.tracks import Tracks
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.artists import Artists
from yt_playlist.repos.tracks import TrackRepo
from yt_playlist.util.matching import identity_key

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def tracks():
    repo = create_autospec(TrackRepo, instance=True, spec_set=True)

    @contextmanager
    def transaction():
        yield

    repo.rating_transaction.side_effect = transaction
    repo.item_rating_inputs.return_value = ({}, {})
    repo.rating_rows.return_value = []
    repo.rating_feedback_rows.return_value = []
    return Tracks(repo, create_autospec(MetadataChoices, instance=True, spec_set=True),
                  create_autospec(Artists, instance=True, spec_set=True))


@pytest.mark.parametrize(('changes', 'same'), [
    ({}, True), ({'duration': 182}, True), ({'duration': 182.01}, False),
    ({'duration': True}, False), ({'duration': 0}, False),
    ({'title': 'Song (Live)'}, False), ({'artist': 'Someone Else'}, False),
    ({'artist': 'Artist Guest'}, True), ({'title': ''}, False),
])
def test_alternate_keeps_version_artist_and_duration_constraints(tracks, changes, same):
    requested = {'video_id': 'original', 'title': 'Song', 'artist': 'Artist', 'duration': 180}
    accepted = {**requested, 'video_id': 'actual', **changes}
    tracks.remember_playback_alternate(3, requested, accepted, evidence={'b': 2, 'a': 1}, now=10)
    tracks.repository.store_playback_alternate.assert_called_once_with(
        3, 'original', 'actual', same_recording=same, evidence_json='{"a": 1, "b": 2}', now=10)


def test_same_upload_does_not_create_an_alternate(tracks):
    tracks.remember_playback_alternate(3, {'video_id': 'v'}, {'video_id': 'v'}, evidence={}, now=10)
    tracks.repository.store_playback_alternate.assert_not_called()


def test_recording_match_rejects_ambiguous_or_different_versions(tracks):
    exact = {'identity_key': 'first', 'title': 'Song (Live)', 'artist': 'Artist'}
    studio = {**exact, 'identity_key': 'studio', 'title': 'Song'}
    tracks.repository.recording_match_rows.return_value = [studio, exact]
    assert tracks.match_recording('Song (Live)', 'Artist') == 'first'
    tracks.repository.recording_match_rows.assert_called_once_with(identity_key('Song (Live)', 'Artist'))
    tracks.repository.recording_match_rows.return_value.append({**exact, 'identity_key': 'second'})
    assert tracks.match_recording('Song (Live)', 'Artist') is None


def test_known_duration_requires_exact_text_and_uses_longest_upload(tracks):
    tracks.repository.duration_match_rows.return_value = [
        {'title': ' Song ', 'artist': 'ARTIST', 'duration_s': 181},
        {'title': 'Song', 'artist': 'Artist', 'duration_s': 180},
        {'title': 'Song', 'artist': 'Artist', 'duration_s': None},
        {'title': 'Song (Live)', 'artist': 'Artist', 'duration_s': 300},
    ]
    assert tracks.known_duration('Song', 'Artist') == 181
    tracks.repository.duration_match_rows.assert_called_once_with(identity_key('Song', 'Artist'))


@pytest.mark.parametrize(('source', 'at', 'status', 'expected', 'action'), [
    ('sync', 9, 'DISLIKE', None, False),
    ('sync', 10, 'DISLIKE', None, False),
    ('sync', 11, 'LIKE', ('LIKE', 'api', 10), False),
    ('sync', 11, 'INDIFFERENT', ('INDIFFERENT', 'sync', 11), False),
    ('action', 10, 'DISLIKE', ('DISLIKE', 'action', 10), False),
    ('api', 9, 'DISLIKE', None, False),
    ('api', 10, 'DISLIKE', ('DISLIKE', 'api', 10), True),
    ('api', 11, 'LIKE', ('LIKE', 'api', 11), False),
])
def test_rating_order_provenance_and_confirmed_action_evidence(tracks, source, at, status, expected, action):
    tracks.repository.item_rating_inputs.return_value = (
        {'video': {'status': 'LIKE', 'source': 'api', 'observed_at': 10}}, {})
    tracks.record_item_rating(3, 'video', status, source=source, now=at)
    observations, actions = tracks.repository.write_item_ratings.call_args.args
    assert observations == ([(3, 'video', *expected)] if expected else [])
    assert actions == ([(3, 'video', 'LIKE', status, source, at)] if action else [])


def test_confirmed_dislike_follows_unban_even_with_frozen_clock(tracks):
    tracks.repository.item_rating_inputs.return_value = ({}, {'video': 10})
    tracks.record_item_rating(3, 'video', 'DISLIKE', source='api', now=10)
    after = nextafter(10, inf)
    tracks.repository.write_item_ratings.assert_called_once_with(
        [(3, 'video', 'DISLIKE', 'api', after)], [(3, 'video', None, 'DISLIKE', 'api', after)])


def test_rating_read_and_write_share_only_the_rating_repository_unit_of_work(tracks):
    events = []

    @contextmanager
    def transaction():
        events.append('begin')
        yield
        events.append('commit')

    tracks.repository.rating_transaction.side_effect = transaction
    tracks.repository.item_rating_inputs.side_effect = lambda *args: (events.append('read') or {}, {})
    tracks.repository.write_item_ratings.side_effect = lambda *args: events.append('write')
    tracks.record_item_ratings(3, {'one': 'LIKE', 'two': 'INDIFFERENT'}, source='sync', now=10)
    assert events == ['begin', 'read', 'write', 'commit']


def rating(key, status, source, at, *, unbanned=False, unbanned_at=None):
    return {'identity_key': key, 'status': status, 'source': source, 'observed_at': at,
            'unbanned': unbanned, 'unbanned_at': unbanned_at}


def test_combined_ratings_keep_dislike_precedence_and_earliest_action(tracks):
    tracks.repository.rating_rows.return_value = [
        rating('one', 'LIKE', 'api', 20), rating('one', 'DISLIKE', 'sync', 30),
        rating('two', 'LIKE', 'sync', 5), rating('two', 'LIKE', 'api', 20),
        rating('two', 'LIKE', 'action', 10), rating('two', 'INDIFFERENT', 'api', 40),
    ]
    combined = tracks.recording_ratings(['one', 'two'])
    assert (combined['one'].status, combined['one'].provenance) == ('DISLIKE', 'sync')
    assert (combined['two'].status, combined['two'].provenance, combined['two'].observed_at) == ('LIKE', 'action', 10)


@pytest.mark.parametrize(('source', 'at', 'unban_at', 'expected'), [
    ('sync', 30, 10, 'INDIFFERENT'), ('action', 30, 10, 'INDIFFERENT'),
    ('api', 10, 10, 'INDIFFERENT'), ('api', 11, 10, 'DISLIKE'),
    ('api', 10, None, 'DISLIKE'), ('sync', 10, None, 'INDIFFERENT'),
])
def test_unban_suppresses_old_or_unconfirmed_dislike(tracks, source, at, unban_at, expected):
    tracks.repository.rating_rows.return_value = [
        rating('key', 'DISLIKE', source, at, unbanned=True, unbanned_at=unban_at)]
    assert tracks.recording_ratings(['key'])['key'].status == expected


def test_pending_projection_uses_combined_feedback_and_action_provenance(tracks):
    tracks.repository.action_rating_rows.return_value = [
        {'identity_key': 'masked', 'video_id': 'like', 'status': 'LIKE', 'observed_at': 10},
        {'identity_key': 'upgrade', 'video_id': 'upgrade', 'status': 'LIKE', 'observed_at': 11},
        {'identity_key': None, 'video_id': 'unknown', 'status': 'DISLIKE', 'observed_at': 12},
        {'identity_key': None, 'video_id': 'neutral', 'status': 'INDIFFERENT', 'observed_at': 13},
    ]
    tracks.repository.rating_rows.return_value = [rating('masked', 'DISLIKE', 'sync', 10),
                                                 rating('upgrade', 'LIKE', 'api', 11)]
    tracks.repository.rating_feedback_rows.return_value = [
        {'item_key': 'masked', 'kind': 'dislike', 'reason': None},
        {'item_key': 'upgrade', 'kind': 'like', 'reason': 'sync'},
    ]
    assert tracks.pending_action_ratings(3) == [
        {'video_id': 'upgrade', 'status': 'LIKE', 'observed_at': 11},
        {'video_id': 'unknown', 'status': 'DISLIKE', 'observed_at': 12},
    ]


def test_metadata_edit_ignores_unchanged_values_and_restores_originals(tracks):
    tracks.repository.metadata_for_video.return_value = {
        'track_id': 7, 'title': 'Edited', 'artist': 'Artist', 'genre': 'Jazz', 'year': '2000',
        'orig_title': 'Original', 'orig_artist': 'Artist'}
    tracks.update_metadata('video', {'title': 'Original', 'genre': 'Jazz', 'year': ''}, observation={})
    tracks.metadata_choices.restore.assert_called_once_with(7, 'title', 'Original')
    tracks.metadata_choices.choose.assert_called_once_with(7, 'mb_year', '')
    tracks.repository.upsert_track.assert_not_called()


def test_unknown_edit_observes_catalog_without_acquiring_library_membership(tracks):
    tracks.repository.metadata_for_video.side_effect = [None, {
        'track_id': 7, 'title': 'Song', 'artist': 'Artist', 'genre': '', 'year': '',
        'orig_title': 'Song', 'orig_artist': 'Artist'}]
    tracks.update_metadata('video', {'genre': 'Jazz'}, observation={'title': 'Song', 'artist': 'Artist'})
    assert tracks.repository.upsert_track.call_args.kwargs == {'library': False}
    tracks.metadata_choices.choose.assert_called_once_with(7, 'genre', 'Jazz')


def test_metadata_title_setter_cannot_blank_title_and_year_can_be_cleared(tracks):
    tracks.set_track_title(7, '   ')
    tracks.metadata_choices.choose.assert_not_called()
    tracks.set_track_title(7, ' Edited ')
    tracks.metadata_choices.choose.assert_called_once_with(7, 'title', 'Edited')
    tracks.set_track_year(7, None)
    assert tracks.metadata_choices.choose.call_args.args == (7, 'mb_year', '')


@pytest.mark.parametrize(('current', 'incoming', 'source', 'chosen', 'expected'), [
    (None, ' https://covers/real ', 'provider', False, 'https://covers/real'),
    ('https://youtube/cover_track_default', 'https://covers/real', 'provider', False, 'https://covers/real'),
    ('https://youtube/real', 'https://covers/real', 'provider', False, None),
    ('https://covers/real', 'https://youtube/new', 'youtube', False, 'https://youtube/new'),
    ('https://covers/real', 'https://youtube/cover_album_default', 'youtube', False, None),
    (None, 'https://youtube/cover_track_default', 'youtube', False, 'https://youtube/cover_track_default'),
    (None, 'https://youtube/cover_track_default', 'provider', False, None),
    ('', 'https://covers/real', 'provider', True, None),
    (None, None, 'provider', False, None),
    (None, '   ', 'provider', False, None),
])
def test_artwork_precedence_uses_catalog_facts_without_database(tracks, current, incoming, source, chosen, expected):
    tracks.repository.artwork_inputs.return_value = {'id': 7, 'thumbnail': current, 'chosen': chosen}
    tracks.set_track_art(99, incoming, source=source)
    if expected is None:
        tracks.metadata_choices.select.assert_not_called()
    else:
        tracks.metadata_choices.select.assert_called_once_with(7, 'thumbnail', expected, source)


def test_unknown_artwork_target_is_ignored(tracks):
    tracks.repository.artwork_inputs.return_value = None
    tracks.set_track_art(99, 'https://covers/real')
    tracks.metadata_choices.select.assert_not_called()
