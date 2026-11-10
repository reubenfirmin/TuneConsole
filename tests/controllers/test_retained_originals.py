from contextlib import nullcontext
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.retained_originals import RetainedOriginals
from yt_playlist.core.original_files import OriginalFiles
from yt_playlist.core.legacy_journals import key
from yt_playlist.repos.retained_originals import RetainedOriginalsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def originals():
    repository = create_autospec(RetainedOriginalsRepo, instance=True, spec_set=True)
    files = create_autospec(OriginalFiles, instance=True, spec_set=True)
    repository.transaction.side_effect = files.retained.side_effect = nullcontext
    repository.inputs.return_value = {'layout': 'journal', 'publication': 'old', 'reference': None}
    repository.subject_ids.return_value = {'artist_id': 7}
    files.journal_entries.return_value = {}
    files.publish_journal.return_value = 'new'
    files.publish_receipts.return_value = [('file', 'digest', 10, 0)]
    return RetainedOriginals(repository, files)


def test_original_journal_receipt_is_insert_once_even_when_interpretation_changes(originals):
    originals.files.journal_entries.return_value = {key('ambiguous_album_artist', '7'): {'state': 'retained'}}
    originals.retain('artist', 'ambiguous_album_artist', '7', 'changed', 'exact release', now=10)
    originals.files.publish_journal.assert_not_called()
    originals.repository.store.assert_not_called()


def test_existing_receipt_must_be_readable_before_any_reconciliation(originals):
    originals.repository.inputs.return_value['reference'] = ('file', 'digest', 10, 0)
    originals.files.read_receipt.side_effect = OSError('missing original')
    with pytest.raises(OSError, match='missing original'):
        originals.retain('artist', 'ambiguous_album_artist', '7', '{}', 'exact release', now=10)
    originals.repository.store.assert_not_called()


def test_journal_retains_exact_payload_subject_and_disposition(originals):
    payload = '{"album_id": 12}'
    originals.retain('artist', 'ambiguous_album_artist', '7', payload, 'exact release', now=10)
    originals.repository.subject_ids.assert_called_once_with('artist', {'album_id': 12, 'artist_id': '7'})
    document = originals.files.publish_journal.call_args.args[2][key('ambiguous_album_artist', '7')]
    assert document['payload'] == payload
    assert document['subjects'] == {'artist_id': 7}
    assert document['archived_at'] == 10
    assert document['retention'] == 'diagnostic'
    assert originals.repository.store.call_args.kwargs == {'publication': 'new', 'reference': None}


@pytest.mark.parametrize('layout', ['inline', 'receipt'])
def test_predecessor_writes_keep_the_installed_owner(originals, layout):
    originals.repository.inputs.return_value = {'layout': layout, 'publication': None, 'reference': None}
    originals.retain('artist', 'ambiguous_album_artist', '7', '{}', 'exact release', now=10)
    assert originals.repository.store.call_args.kwargs == {
        'publication': None, 'reference': ('file', 'digest', 10, 0) if layout == 'receipt' else None}
    originals.files.publish_journal.assert_not_called()


def test_failed_file_publication_does_not_publish_an_owner(originals):
    originals.files.publish_journal.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        originals.retain('artist', 'ambiguous_album_artist', '7', '{}', 'exact release', now=10)
    originals.repository.store.assert_not_called()


@pytest.mark.parametrize('domain,kind,payload,reason', [
    ('unknown', 'kind', '{}', 'reason'), ('artist', '', '{}', 'reason'),
    ('artist', 'kind', {}, 'reason'), ('artist', 'kind', '{}', '')])
def test_invalid_evidence_cannot_open_a_write(originals, domain, kind, payload, reason):
    with pytest.raises(ValueError, match='Invalid retained original'):
        originals.retain(domain, kind, '7', payload, reason, now=10)
    originals.repository.transaction.assert_not_called()
