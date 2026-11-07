from contextlib import contextmanager
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.road_trip import RoadTrips
from yt_playlist.repos.road_trip import RoadTripRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def trips():
    repository = create_autospec(RoadTripRepo, instance=True, spec_set=True)
    writing = False

    @contextmanager
    def transaction():
        nonlocal writing
        assert not writing
        writing = True
        try:
            yield
        finally:
            writing = False

    @contextmanager
    def retained():
        assert not writing
        yield

    def publish(*args):
        assert not writing, 'Draft file publication must run outside the SQL transaction'
        return 'file', 'digest', 50

    def read(*args, **kwargs):
        assert not writing
        return {'revision':'untrusted-file-revision', 'picks':['stale'], 'banned':['stale'], 'pool':[]}

    repository.transaction.side_effect = transaction
    repository.uses_memberships.return_value = True
    repository.uses_draft_files.return_value = True
    repository.artist_candidates.return_value = set()
    repository.genre_candidates.return_value = set()
    repository.genre_key_exists.return_value = False
    repository.draft_owner.return_value = None
    repository.master_account.return_value = 4
    repository.captured_video.return_value = (10, 20)
    repository.save_draft.return_value = True
    repository.save_legacy_draft.return_value = True
    return RoadTrips(repository, Mock(retained=retained, publish=Mock(side_effect=publish), read=Mock(side_effect=read)),
                     Mock(return_value=json.dumps({'revision':'prepared'})))


def save_recipe(trips, **kwargs):
    return trips.save_road_trip_recipe(None, 'Trip', kwargs.pop('own_pct', 50), kwargs.pop('artists', []),
        kwargs.pop('genres', []), kwargs.pop('minutes', 60), kwargs.pop('now', 100), **kwargs)


@pytest.mark.parametrize('values', [
    {'own_pct':True}, {'own_pct':101}, {'familiarity_pct':-1}, {'minutes':14},
    {'minutes':721}, {'minutes':60.0}, {'now':float('inf')}, {'now':float('nan')},
])
def test_recipe_contract_rejects_invalid_controls_before_any_write(trips, values):
    with pytest.raises(ValueError):
        save_recipe(trips, **values)
    trips.repository.transaction.assert_not_called()
    trips.repository.save_recipe.assert_not_called()


def test_same_name_artist_candidates_remain_unresolved(trips):
    trips.repository.artist_candidates.side_effect = [{1, 2}, {3}]
    save_recipe(trips, artists=['Ambiguous', '@artist:3'])
    assert trips.repository.save_recipe.call_args.kwargs['memberships'] == {
        'artists':[(3, 1)], 'genres':[],
        'unresolved_artists':[(0, 'Ambiguous', 'unknown_or_ambiguous_artist')], 'unresolved_genres':[]}


def test_aliases_of_the_same_artist_cannot_be_selected_twice(trips):
    trips.repository.artist_candidates.return_value = {3}
    with pytest.raises(ValueError, match='same artist twice'):
        save_recipe(trips, artists=['Name', '@artist:3'])
    trips.repository.save_recipe.assert_not_called()


def test_custom_genre_is_shared_across_roles_but_order_and_role_stay_explicit(trips):
    save_recipe(trips, genres=['New genre'], blacklist_genres=['NEW GENRE'])
    trips.repository.insert_custom_genre.assert_called_once_with('new genre', 'New genre')
    assert trips.repository.save_recipe.call_args.kwargs['memberships']['genres'] == [
        ('new genre', 'include', 0), ('new genre', 'exclude', 0)]


@pytest.mark.parametrize('choices', [{1, 2}, set()])
def test_ambiguous_or_repeated_genre_cannot_modify_the_recipe(trips, choices):
    trips.repository.genre_candidates.return_value = choices
    with pytest.raises(ValueError, match='Ambiguous genre|same genre twice'):
        save_recipe(trips, genres=['New', 'NEW'])
    trips.repository.save_recipe.assert_not_called()
    trips.repository.insert_custom_genre.assert_not_called()


def test_legacy_recipe_keeps_its_opaque_lists_without_new_identity_admission(trips):
    trips.repository.uses_memberships.return_value = False
    save_recipe(trips, artists=['Unknown', 'Unknown'], genres=['Legacy'])
    trips.repository.transaction.assert_not_called()
    assert trips.repository.save_recipe.call_args.kwargs == {}
    assert trips.repository.save_recipe.call_args.args[1]['familiarity_pct'] == 50


def test_draft_publishes_recovery_after_admission_then_supplies_exact_ordered_references(trips):
    state = {'revision':'next', 'building':True, 'picks':['video'], 'banned':['video'],
             'pool':[{'video_id':'video', 'title':'Old'}],
             'picked':[{'video_id':'video', 'title':'Captured', 'artist':'Credit'}]}
    trips.save_road_trip_draft(2, state, 100)
    trips.repository.captured_video.assert_called_once_with('video', identity_key='@unresolved-video:video',
        title='Captured', artist='Credit', now=100)
    args = trips.repository.save_draft.call_args.args
    assert args[0] == 2 and args[2:6] == (4, 'next', 'building', ('file', 'digest', 50))
    assert args[6] == [('picked', 0, 20, 10, 'video'), ('excluded', 0, 20, 10, 'video')]
    published = trips.recovery.publish.call_args.args
    assert published[:2] == ('road-trip-draft', args[1])
    assert 'picks' not in published[2] and 'banned' not in published[2]
    assert state['picks'] == ['video'] and state['banned'] == ['video']


@pytest.mark.parametrize('state', [{'picks':[]}, {'revision':''}, {'revision':'r', 'picks':[None]}])
def test_invalid_native_draft_cannot_create_an_identity_or_file(trips, state):
    with pytest.raises(ValueError):
        trips.save_road_trip_draft(2, state, 100)
    trips.repository.captured_video.assert_not_called()
    trips.recovery.publish.assert_not_called()


@pytest.mark.parametrize('state', [{'revision':'next', 'picks':['video']}, {'picks':[None]}])
def test_stale_worker_cannot_admit_videos_or_publish_a_file(trips, state):
    trips.repository.draft_owner.return_value = {'id':'owner', 'account_id':4, 'revision':'newer'}
    assert not trips.replace_road_trip_draft(2, state, 100, expected_revision='old')
    trips.repository.captured_video.assert_not_called()
    trips.recovery.publish.assert_not_called()


def test_changed_revision_during_file_write_is_rejected_by_the_final_sql_comparison(trips):
    trips.repository.draft_owner.return_value = {'id':'owner', 'account_id':4, 'revision':'old'}
    trips.repository.save_draft.return_value = False
    assert not trips.replace_road_trip_draft(2, {'revision':'next'}, 100, expected_revision='old')
    assert trips.repository.save_draft.call_args.kwargs == {'expected_revision':'old', 'conditional':True}
    trips.recovery.publish.assert_called_once()


def test_failed_file_write_cannot_replace_the_durable_draft(trips):
    trips.recovery.publish.side_effect = OSError('controlled file failure')
    with pytest.raises(OSError, match='controlled file failure'):
        trips.save_road_trip_draft(2, {'revision':'next'}, 100)
    trips.repository.save_draft.assert_not_called()


def test_existing_unknown_author_is_retained_without_choosing_a_new_account(trips):
    trips.repository.draft_owner.return_value = {'id':'owner', 'account_id':None, 'revision':'old'}
    trips.save_road_trip_draft(2, {'revision':'next'}, 100)
    trips.repository.master_account.assert_not_called()
    assert trips.repository.save_draft.call_args.args[2] is None


def test_new_draft_needs_an_active_master_before_any_file_write(trips):
    trips.repository.master_account.return_value = None
    with pytest.raises(ValueError, match='Choose an active account'):
        trips.save_road_trip_draft(2, {'revision':'next'}, 100)
    trips.recovery.publish.assert_not_called()


def test_sql_choices_and_revision_override_recovery_contents(trips):
    trips.repository.draft_facts.return_value = {'recipe_id':2, 'row':{
        'id':'owner', 'revision':'sql', 'recovery_filename':'file', 'recovery_digest':'digest', 'recovery_bytes':50},
        'selections':[('excluded','b'), ('picked','a')]}
    assert trips.latest_road_trip_draft() == {
        'recipe_id':2, 'state':{'revision':'sql', 'picks':['a'], 'banned':['b'], 'pool':[]}}


def test_prepared_legacy_read_uses_its_manifest_and_compare_save_uses_original_sql_bytes(trips):
    trips.repository.uses_draft_files.return_value = False
    trips.repository.draft_facts.return_value = {
        'recipe_id':2, 'raw':'original bytes', 'prepared':{'recipe_id':2}, 'parts':[(0, 'file', 'digest', 50)]}
    assert trips.replace_road_trip_draft(2, {'revision':'next'}, 100, expected_revision='prepared')
    trips.repository.save_legacy_draft.assert_called_once_with(2, json.dumps({'revision':'next'}), 100,
        expected_raw='original bytes', conditional=True)
    trips.recovery.publish.assert_not_called()


def test_missing_draft_is_not_recreated_by_a_legacy_worker(trips):
    trips.repository.uses_draft_files.return_value = False
    trips.repository.draft_facts.return_value = None
    assert not trips.replace_road_trip_draft(2, {'revision':'next'}, 100, expected_revision='old')
    trips.repository.save_legacy_draft.assert_not_called()
