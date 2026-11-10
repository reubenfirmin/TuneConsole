"""Radio checkpoint orchestration uses mocked SQL, catalog and file owners."""
from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.radio_checkpoints import RadioCheckpoints
from yt_playlist.core.radio_files import RadioFiles
from yt_playlist.repos.radio_checkpoints import RadioCheckpointRepo
from yt_playlist.controllers.settings import Settings
from yt_playlist.controllers.tracks import Tracks

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def checkpoints():
    repo = create_autospec(RadioCheckpointRepo, instance=True, spec_set=True)
    settings = create_autospec(Settings, instance=True, spec_set=True)
    tracks = create_autospec(Tracks, instance=True, spec_set=True)
    files = create_autospec(RadioFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    repo.save_facts.return_value = {'native': True, 'head': None, 'existing': None,
                                   'account_exists': True, 'operations': True}
    repo.queue_entries.return_value = {}
    repo.publish.return_value = ('episode', 1)
    files.publish.return_value = ('checkpoint', 'digest', 100)
    return RadioCheckpoints(repo, settings, tracks, files, nullcontext)


def state(**values):
    return {'version': 2, 'phase': 'playing', 'episode_id': 'episode', 'identity': 3,
            'playlist': 'RADIO', 'tracks': [], **values}


def reference(**values):
    return {'id': 'episode', 'revision': 1, 'account_id': 3, 'status': 'playing',
            'recovery_filename': 'checkpoint', 'recovery_digest': 'digest', 'recovery_bytes': 100, **values}


def test_catalog_commits_then_file_then_radio_sql_with_final_recording_references(checkpoints):
    owner = checkpoints
    owner.repository.queue_entries.side_effect = [{}, {'video': (7, 8)}]
    order = Mock()
    order.attach_mock(owner.tracks.observe_track_entry, 'catalog')
    order.attach_mock(owner.files.publish, 'file')
    order.attach_mock(owner.repository.publish, 'sql')
    value = state(tracks=[{'video_id': 'video', 'title': 'Song', 'artist': 'Artist'}], position=0)
    assert owner.save(json.dumps(value), expected=None) == ('episode', 1)
    assert [call[0] for call in order.mock_calls] == ['catalog', 'file', 'sql']
    owner.tracks.observe_track_entry.assert_called_once_with(
        'video', 'Song', 'Artist', preferred_key='@unresolved-video:video')
    assert owner.repository.publish.call_args.args[4] == [(0, 7, 8, 'video', 'current')]


def test_existing_upload_is_not_readmitted_and_queue_duplicates_keep_positions(checkpoints):
    checkpoints.repository.queue_entries.return_value = {'video': (7, 8)}
    value = state(tracks=[{'video_id': 'video'}, {'video_id': 'video'}], position=1)
    checkpoints.save(json.dumps(value), expected=None)
    checkpoints.tracks.observe_track_entry.assert_not_called()
    assert checkpoints.repository.publish.call_args.args[4] == [
        (0, 7, 8, 'video', 'played'), (1, 7, 8, 'video', 'current')]


def test_failed_file_leaves_catalog_admission_and_does_not_select_checkpoint(checkpoints):
    checkpoints.repository.queue_entries.side_effect = [{}, {'video': (7, 8)}]
    checkpoints.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError):
        checkpoints.save(json.dumps(state(tracks=[{'video_id': 'video'}])), expected=None)
    checkpoints.tracks.observe_track_entry.assert_called_once()
    checkpoints.repository.publish.assert_not_called()


def test_stale_checkpoint_does_not_admit_tracks_or_publish_files(checkpoints):
    checkpoints.repository.save_facts.return_value['head'] = reference(revision=2)
    with pytest.raises(ValueError, match='changed.*worker'):
        checkpoints.save(json.dumps(state(tracks=[{'video_id': 'new'}])), expected=('episode', 1))
    checkpoints.repository.queue_entries.assert_not_called()
    checkpoints.tracks.observe_track_entry.assert_not_called()
    checkpoints.files.publish.assert_not_called()


@pytest.mark.parametrize('change', [{'identity': '3'}, {'episode_id': ''}, {'episode_id': []}])
def test_invalid_owner_does_not_publish_checkpoint(checkpoints, change):
    with pytest.raises(ValueError, match='Radio checkpoint requires'):
        checkpoints.save(json.dumps(state(**change)), expected=None)
    checkpoints.files.publish.assert_not_called()
    checkpoints.repository.publish.assert_not_called()


def test_overlapping_operations_fail_before_catalog_or_files(checkpoints):
    value = state(pending={'video_id': 'new'}, replacement={'allowed': []})
    with pytest.raises(ValueError, match='only one active'):
        checkpoints.save(json.dumps(value), expected=None)
    checkpoints.repository.queue_entries.assert_not_called()
    checkpoints.files.publish.assert_not_called()


def test_untouched_import_adopts_only_exact_predecessor_and_retains_original(checkpoints):
    original = state()
    checkpoints.repository.save_facts.return_value.update(head=reference(), existing={'account_id': 3, 'revision': 1})
    checkpoints.files.read.return_value = dict(original)
    checkpoints.save(json.dumps(state(phase='stopped')), expected=None, predecessor=original)
    assert checkpoints.repository.publish.call_args.kwargs['expected'] == ('episode', 1)
    assert checkpoints.repository.publish.call_args.args[2] == 2


def test_changed_import_does_not_adopt_predecessor(checkpoints):
    checkpoints.repository.save_facts.return_value.update(head=reference(), existing={'account_id': 3, 'revision': 1})
    checkpoints.files.read.return_value = state(error='changed')
    with pytest.raises(ValueError, match='changed.*worker'):
        checkpoints.save(json.dumps(state()), expected=None, predecessor=state())
    checkpoints.files.publish.assert_not_called()


def test_native_queue_projects_current_identity_without_losing_captured_metadata(checkpoints):
    checkpoints.repository.checkpoint_facts.return_value = {'native': True, 'header': reference(), 'queue': [
        {'position': 0, 'video_id': 'video', 'identity_key': 'new-key', 'state': 'queued'}]}
    checkpoints.repository.operation_facts.return_value = None
    checkpoints.files.read.return_value = state(tracks=[{'video_id': 'old-video', 'key': 'old-key',
                                                        'provider_details': {'original': True}}])
    value, revision = checkpoints.load()
    assert revision == ('episode', 1)
    assert value['tracks'] == [{'video_id': 'video', 'key': 'new-key', 'provider_details': {'original': True}}]


def test_prepared_original_and_operation_remain_file_backed(checkpoints):
    checkpoints.repository.checkpoint_facts.return_value = {'native': False, 'prepared': True, 'header': {'source_present': 1}}
    checkpoints.files.read_prepared.return_value = json.dumps(state(pending={'video_id': 'old'}, pending_sent=False))
    header = reference(source='radio_current_session', kind='append', sent=1, started_at=12,
        receipt_status='confirmed', receipt_video_id_snapshot='new', receipt_handle_snapshot='handle',
        adding_video_id_snapshot='new')
    checkpoints.repository.operation_facts.return_value = {'native': False, 'header': header, 'roles': {}}
    checkpoints.files.read.return_value = state(pending={'video_id': 'old', 'captured': True}, pending_sent=False,
                                               pending_result={'status': 'pending', 'opaque': 9})
    value, revision = checkpoints.load()
    assert revision is None
    assert value['pending'] == {'video_id': 'new', 'captured': True}
    assert value['pending_sent'] is True
    assert value['pending_result'] == {'status': 'confirmed', 'opaque': 9}
    checkpoints.settings.get_setting.assert_not_called()


def test_legacy_origins_preserve_order_and_deduplicate_across_keys(checkpoints):
    checkpoints.repository.origin_facts.return_value = {'native': False, 'ids': None,
        'values': {'radio_session_playlist_ids': '["one", "two"]', 'radio_playlist_ytm': 'one',
                   'radio_playlist_a_ytm': 'three'}}
    checkpoints.remember_playlists(['two', 'four'])
    checkpoints.settings.set_setting.assert_called_once_with('radio_session_playlist_ids', '["one", "two", "three", "four"]')
    checkpoints.repository.remember_playlists.assert_not_called()


def test_invalid_origin_does_not_write_partial_batch(checkpoints):
    with pytest.raises(ValueError, match='provider playlist ID'):
        checkpoints.remember_playlists(['one', ''])
    checkpoints.repository.remember_playlists.assert_not_called()
    checkpoints.settings.set_setting.assert_not_called()
