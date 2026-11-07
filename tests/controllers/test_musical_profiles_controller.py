"""Profile policy and file/SQL ordering without a test database."""
from contextlib import nullcontext
import hashlib
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.musical_profiles import MusicalProfiles
from yt_playlist.controllers.recommendation_refresh import RecommendationRefresh
from yt_playlist.core.audio_metadata_files import AudioMetadataFiles
from yt_playlist.core.profile_files import ProfileFiles
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.controllers.vibe_predictions import VibePredictions

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def profiles():
    repository = create_autospec(MusicalProfilesRepo, instance=True, spec_set=True)
    files = create_autospec(ProfileFiles, instance=True, spec_set=True)
    metadata = create_autospec(AudioMetadataFiles, instance=True, spec_set=True)
    predictions = create_autospec(VibePredictions, instance=True, spec_set=True)
    refresh = create_autospec(RecommendationRefresh, instance=True, spec_set=True)
    files.retained.side_effect = metadata.retained.side_effect = nullcontext
    predictions.overlay.side_effect = lambda values: values
    return MusicalProfiles(repository, files, metadata, predictions, refresh)


def reference(key, digest='digest', raw='{}'):
    return key, raw, 'version', digest, key, None, None


def test_changed_source_scope_retries_without_opening_evidence(profiles):
    profiles.repository.source_facts.return_value = None
    assert profiles.source_input('source', observation_ids=(3,)) is None
    profiles.metadata.source_values.assert_not_called()


def test_source_decoding_uses_only_captured_references_and_supplied_byte_bound(profiles):
    source = dict(provider='provider', scope='artist', entity_id='id', reference={'revision': 5})
    profiles.repository.source_facts.return_value = source, (2, 0), [7]
    profiles.metadata.source_values.return_value = {'energy': .4}
    value, pending, ids = profiles.source_input('source', max_bytes=123, observation_ids=('runtime', 5))
    assert value == dict(provider='provider', scope='artist', entity_id='id', observations={'energy': .4})
    assert (pending, ids) == ((2, 0), [7])
    assert 'reference' in source
    profiles.metadata.source_values.assert_called_once_with({'revision': 5}, max_bytes=123)


def test_source_file_precedes_sql_fanout_and_acknowledgement(profiles):
    events = []
    profiles.repository.publication_owners.return_value = {'source': 'source'}
    profiles.files.write.side_effect = lambda *args: events.append('file') or ['reference']
    profiles.repository.publish_source.side_effect = lambda *args, **kwargs: events.append('sql')
    profiles.publish_source('source', (2, 0), [7], '{}', 'v', 'digest', 10)
    assert events == ['file', 'sql']
    profiles.repository.publish_source.assert_called_once_with(
        'source', (2, 0), [7], '{}', 'v', 'digest', 10, 32, references=['reference'])


def test_failed_source_file_does_not_acknowledge_pending_work(profiles):
    profiles.repository.publication_owners.return_value = {'source': 'source'}
    profiles.files.write.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        profiles.publish_source('source', (2, 0), [7], '{}', 'v', 'digest', 10)
    profiles.repository.publish_source.assert_not_called()


def test_conflicting_explicit_choices_block_inherited_values(profiles):
    refs = [reference('artist'), reference('release')]
    profiles.repository.recording_facts.return_value = {
        'song': (refs, {'genre': {'"Jazz"', '"Metal"'}, 'energy': {'null'}, 'tempo': {'123'}}, True)}
    profiles.files.read_page.return_value = {'artist': {'first': True}, 'release': {'second': True}}
    assert profiles.recording_inputs(['song'], max_bytes=100, source_scopes=('artist', 'release')) == {
        'song': ([{'first': True}, {'second': True}], {'genre': None, 'energy': None, 'tempo': 123}, True)}
    profiles.repository.recording_facts.assert_called_once_with(
        ['song'], source_scopes=('artist', 'release'), max_sources=64, max_link_scan=4096)
    profiles.files.read_page.assert_called_once_with('source', refs, max_bytes=100)


@pytest.mark.parametrize('method', ['by_keys', 'recording_inputs'])
def test_oversized_profile_pages_fail_before_sql_or_files(profiles, method):
    with pytest.raises(ValueError, match='512'):
        getattr(profiles, method)(list(map(str, range(513))))
    profiles.repository.profile_references.assert_not_called()
    profiles.repository.recording_facts.assert_not_called()
    profiles.files.read_page.assert_not_called()


def test_prediction_overlay_is_separate_from_profile_file_decoding(profiles):
    profiles.repository.profile_references.return_value = [reference('song')]
    profiles.files.read_page.return_value = {'song': {'version': 'v'}}
    profiles.predictions.overlay.side_effect = None
    profiles.predictions.overlay.return_value = {'song': {'version': 'v', 'vibes': 'current'}}
    assert profiles.by_keys(['song'])['song']['vibes'] == 'current'
    profiles.predictions.overlay.assert_called_once_with({'song': {'version': 'v'}})


def test_recording_publication_removes_derived_vibes_and_notifies_after_sql(profiles):
    events = []
    profiles.repository.publication_owners.return_value = {'song': 17}
    profiles.files.write.side_effect = lambda *args: events.append('file') or ['reference']
    profiles.repository.publish_recordings.side_effect = lambda *args, **kwargs: events.append('sql') or True
    profiles.refresh.publish_notification.side_effect = lambda: events.append('notify')
    profiles.publish_recordings([('song', '{"energy":0.4,"vibes":{"old":true}}', 'v', 'old')], 10)
    raw = '{"energy":0.4}'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert events == ['file', 'sql', 'notify']
    profiles.files.write.assert_called_once_with('recording', [('song', 17, raw, 'v', digest)])
    profiles.repository.publish_recordings.assert_called_once_with(
        [('song', raw, 'v', digest)], 10, references=['reference'])


def test_unchanged_publication_does_not_notify(profiles):
    profiles.repository.publication_owners.return_value = {}
    profiles.repository.publish_recordings.return_value = False
    profiles.publish_recordings([('gone', None, 'v', None)], 10)
    profiles.refresh.publish_notification.assert_not_called()


def test_legacy_inline_format_has_no_file_publication(profiles):
    profiles.repository.publication_owners.return_value = None
    profiles.publish_recordings([('song', '{}', 'v', 'digest')], 10)
    profiles.files.write.assert_not_called()
    profiles.repository.publish_recordings.assert_called_once_with(
        [('song', '{}', 'v', 'digest')], 10, references=[])


def test_failed_recording_file_does_not_write_sql_or_notify(profiles):
    profiles.repository.publication_owners.return_value = {'song': 17}
    profiles.files.write.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        profiles.publish_recordings([('song', '{}', 'v', 'digest')], 10)
    profiles.repository.publish_recordings.assert_not_called()
    profiles.refresh.publish_notification.assert_not_called()


def test_enclosing_transaction_is_rejected_before_file_work(profiles):
    profiles.repository.assert_idle.side_effect = RuntimeError('outside a transaction')
    with pytest.raises(RuntimeError, match='outside'):
        profiles.publish_recordings([('song', '{}', 'v', 'digest')], 10)
    profiles.files.write.assert_not_called()


def test_provenance_budget_omits_new_or_unaffordable_sources(profiles):
    rows = [reference('first', 'used', '{"old":true}'), reference('second', 'other')]
    profiles.repository.provenance_references.return_value = rows
    profiles.files.read_page.return_value = {'first': {'old': True}}
    assert profiles.provenance_sources('song', ['used', 'other', 'missing'], max_bytes=12) == ([{'old': True}], True)
    profiles.files.read_page.assert_called_once_with('source', rows[:1], max_bytes=12)
    profiles.repository.provenance_references.assert_called_once_with('song', ['used', 'other', 'missing'], limit=16)


def test_empty_provenance_does_not_open_sql_or_files(profiles):
    assert profiles.provenance_sources('song', []) == ([], False)
    profiles.repository.provenance_references.assert_not_called()
    profiles.files.read_page.assert_not_called()


def test_readiness_preserves_explicit_page_hash_and_missing_profile_fits(profiles):
    rows = [('song', 'v', 'digest', 'relax', .8, .5, 'provider', 'stamp'),
            ('unprepared', None, None, 'relax', .9, .9, 'provider', 'stamp')]
    profiles.repository.readiness_rows.return_value = rows
    fingerprint, values = profiles.readiness_state(['song', 'unprepared', 'song'])
    assert fingerprint == hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()
    assert values['song']['vibes']['fits']['relax']['score'] == .8
    assert values['unprepared']['vibes']['fits'] == {}
    profiles.repository.readiness_rows.assert_called_once_with(['song', 'unprepared'])
