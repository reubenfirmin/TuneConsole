"""Publication order and maintenance decisions without SQLite or managed files."""
from contextlib import nullcontext
import json
import time
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_impressions import RecommendationImpressions
from yt_playlist.core import fao, served_facts
from yt_playlist.core.served_files import ServedFiles
from yt_playlist.repos.rec_ledger import RecLedgerRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def impressions():
    repository = create_autospec(RecLedgerRepo, instance=True, spec_set=True)
    files = create_autospec(ServedFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.collector.side_effect = lambda: nullcontext(True)
    return RecommendationImpressions(repository, files, nullcontext)


def event(trace=None):
    return ('request', 'home', 'lane', None, 'song', 1, .5, 'version', json.dumps(trace or {}), 100)


def test_ids_commit_before_files_and_reporting_owners_commit_last(impressions):
    trace = {'pool_policy': 'pool', 'candidate': {'artist': 'Captured', 'coverage': {'genre': True}}}
    row = event(trace)
    order = []
    impressions.repository.reserve_impression_ids.side_effect = lambda rows: order.append('ids') or [(7, 0)]
    impressions.files.publish_many.side_effect = lambda rows: order.append('files')
    impressions.repository.insert_reserved_impressions.side_effect = lambda *args, **kwargs: order.append('owners')
    impressions.record([row], versions=['version', 'pool'])
    assert order == ['ids', 'files', 'owners']
    impressions.files.publish_many.assert_called_once_with([(7, 100, trace)])
    prepared = impressions.repository.insert_reserved_impressions.call_args.args[0]
    assert prepared[0][0] == 7 and json.loads(prepared[0][1][8]) == {'pool_policy': 'pool'}
    assert prepared[0][2] == served_facts.extract(trace)


def test_file_failure_keeps_reporting_owners_unpublished(impressions):
    impressions.repository.reserve_impression_ids.return_value = [(7, 0)]
    impressions.files.publish_many.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        impressions.record([event()], versions=['version'])
    impressions.repository.insert_reserved_impressions.assert_not_called()


def test_empty_batch_does_no_work(impressions):
    impressions.record([], versions=[])
    impressions.repository.reserve_impression_ids.assert_not_called()
    impressions.files.retained.assert_not_called()


def test_report_keeps_frozen_typed_facts_after_diagnostic_expiry(impressions):
    trace = {'policy_kind': 'live', 'candidate': {'artist': 'Captured', 'last_played': None}}
    fact = dict(zip(served_facts.FIELDS, served_facts.extract(trace))) | {'diagnostic_expired': 1}
    impressions.repository.served_recommendation_facts.return_value = {
        'id': 1, 'served_at': 100, 'provenance': '{"pool_policy":"old"}', 'facts': fact}
    impressions.files.diagnostic.return_value = None
    result = impressions.facts(1)
    assert result['diagnostic_status'] == 'expired'
    assert result['provenance']['candidate']['artist'] == 'Captured'
    assert result['provenance']['pool_policy'] == 'old'
    assert result['provenance']['candidate']['last_played'] is None


def test_legacy_or_missing_report_does_not_open_a_diagnostic(impressions):
    impressions.repository.served_recommendation_facts.return_value = None
    assert impressions.facts(1) is None
    impressions.repository.served_recommendation_facts.return_value = {'id': 2, 'provenance': '{"old":true}', 'facts': None}
    assert impressions.facts(2)['provenance'] == {'old': True}
    impressions.files.diagnostic.assert_not_called()


def test_expiry_is_committed_before_file_deletion(impressions):
    events = []
    impressions.repository.expire_diagnostics.side_effect = lambda *args, **kwargs: events.append('expiry') or [1, 2]
    impressions.files.delete_many.side_effect = lambda rows: events.append('delete')
    impressions.files.orphan_candidates.return_value = [(3, 'three'), (4, 'four')]
    impressions.repository.active_diagnostics.return_value = {4}
    impressions.maintain(10_000_000, before=1_000_000)
    assert events == ['expiry', 'delete']
    impressions.repository.expire_diagnostics.assert_called_once_with(10_000_000 - served_facts.RETAIN_SECONDS, limit=256)
    impressions.files.delete_many.assert_called_once_with([1, 2])
    impressions.files.delete_orphans.assert_called_once_with([(3, 'three')])


def test_retained_reader_defers_every_maintenance_write(impressions):
    impressions.files.collector.side_effect = lambda: nullcontext(False)
    impressions.maintain(10_000_000, before=1_000_000)
    impressions.repository.expire_diagnostics.assert_not_called()
    impressions.repository._prune_impression_batch.assert_not_called()
    impressions.files.delete_many.assert_not_called()


def test_overlapping_cleanup_preserves_owned_and_replaced_diagnostics(impressions, tmp_path):
    files = ServedFiles(tmp_path / 'served')
    impressions.files = files
    files.publish_many([(identity, 100, {}) for identity in (1, 2, 3, 4)])
    gone, owned, replaced, orphan = (files.root / served_facts.relative(identity) for identity in (1, 2, 3, 4))
    replacement = b'replacement contents'
    impressions.repository.expire_diagnostics.return_value = []

    def owners(candidates):
        assert set(candidates) == {1, 2, 3, 4}
        gone.unlink()
        fao.write(replaced, replacement)
        return {2}

    impressions.repository.active_diagnostics.side_effect = owners
    impressions.maintain(time.time() + served_facts.RETAIN_SECONDS + 10, before=100)
    assert not gone.exists() and not orphan.exists()
    assert owned.is_file()
    assert fao.read(replaced).bytes == replacement


def test_failed_expiry_preserves_diagnostics(impressions):
    impressions.repository.expire_diagnostics.side_effect = RuntimeError('sql failure')
    with pytest.raises(RuntimeError, match='sql failure'):
        impressions.maintain(10_000_000, before=1_000_000)
    impressions.files.delete_many.assert_not_called()
