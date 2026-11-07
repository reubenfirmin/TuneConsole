"""Verified corrections compose explicit owners without database fixtures."""
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.controllers.listening_calendar import ListeningCalendar
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.rec_model import RecommendationModel
from yt_playlist.controllers.recording_associations import RecordingAssociations
from yt_playlist.controllers.recording_keys import RecordingKeys
from yt_playlist.repos.tracks import TrackRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def associations():
    repo = create_autospec(TrackRepo, instance=True, spec_set=True)
    repo.association_subjects.return_value = dict(previous=7, target=12, previous_key='old', target_key='target', remaining=None)
    repo.rating_rows.return_value = []
    repo.rating_feedback_rows.return_value = []
    repo.record_association.return_value = 20
    repo.redirect_recording.return_value = ['alias']
    return RecordingAssociations(repo, create_autospec(MetadataChoices, instance=True, spec_set=True),
        create_autospec(RecordingKeys, instance=True, spec_set=True),
        create_autospec(Discovery, instance=True, spec_set=True),
        create_autospec(ListeningCalendar, instance=True, spec_set=True),
        create_autospec(RecommendationModel, instance=True, spec_set=True))


@pytest.mark.parametrize('equivalent', [False, True])
def test_only_explicit_equivalence_copies_metadata(associations, equivalent):
    associations.associate('video', 10, source='review', evidence={'matched': True}, now=100, same_recording=equivalent)
    if equivalent:
        associations.metadata.reuse_recording_selections.assert_called_once_with(7, 12)
    else:
        associations.metadata.reuse_recording_selections.assert_not_called()
    associations.repository.assign_recording.assert_called_once_with('video', 7, 12, 'target')
    associations.calendar.reconcile_recordings.assert_called_once_with({7, 12})
    associations.model.reassociate_rating_feedback.assert_called_once_with({}, {'old': [], 'target': []})


def test_file_preparation_finishes_before_changing_the_upload(associations):
    order = Mock()
    for action, name in ((associations.discovery.prepare_track_reassociation, 'discovery'),
                         (associations.keys.prepare_reassociation, 'documents'),
                         (associations.repository.assign_recording, 'assignment')):
        order.attach_mock(action, name)
    associations.keys.prepare_reassociation.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        associations.associate('video', 12, source='review', evidence={'matched': True}, now=100)
    assert [call[0] for call in order.mock_calls] == ['discovery', 'documents']
    associations.repository.redirect_recording.assert_not_called()
    associations.model.reassociate_rating_feedback.assert_not_called()


def test_partial_correction_keeps_the_old_recording_and_its_documents(associations):
    associations.repository.association_subjects.return_value['remaining'] = 8
    associations.associate('video', 12, source='review', evidence={'matched': True}, now=100)
    associations.keys.prepare_reassociation.assert_not_called()
    associations.keys.finish_reassociation.assert_not_called()
    associations.repository.redirect_recording.assert_not_called()
    associations.discovery.finish_track_reassociation.assert_called_once()


def test_failed_metadata_reuse_prevents_receipt_and_assignment(associations):
    associations.metadata.reuse_recording_selections.side_effect = RuntimeError('interrupted')
    with pytest.raises(RuntimeError, match='interrupted'):
        associations.associate('video', 12, source='review', evidence={'matched': True}, now=100, same_recording=True)
    associations.repository.record_association.assert_not_called()
    associations.repository.assign_recording.assert_not_called()


def test_existing_association_has_no_side_effects(associations):
    associations.repository.association_subjects.return_value['previous'] = 12
    associations.associate('video', 12, source='review', evidence={'matched': True}, now=100)
    associations.repository.record_association.assert_not_called()
    associations.discovery.prepare_track_reassociation.assert_not_called()
