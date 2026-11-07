from contextlib import nullcontext
import hashlib
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.prepared_content import PreparedContent
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.repos.rec_refresh import RecRefreshRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def preparation():
    repository = create_autospec(PreparedContentRepo, instance=True, spec_set=True)
    repository.capture_reads.side_effect = lambda keys: nullcontext()
    repository.capture_facts.return_value = {
        'library': {'library', 'both'},
        'content': {'library': ('Rock', '2001-05-03'), 'both': ('', 'unknown')},
        'audio': {'library': {'energy': 0, 'bpm': 120}}, 'previous': {},
    }
    discovery = create_autospec(DiscoveryRepo, instance=True, spec_set=True)
    discovery.discovered_tracks_by_keys.return_value = {
        'both': {'genre': 'Jazz', 'year': '1999', 'audio': {'bpm': 90}},
        'discovery': {'genre': '', 'year': None},
    }
    refresh = create_autospec(RecRefreshRepo, instance=True, spec_set=True)
    return PreparedContent(repository, discovery, refresh)


def test_builds_explicit_cohorts_and_preserves_pending_order(preparation):
    pending = [('both', 4), ('library', 2), ('discovery', 7), ('missing', 8)]
    rows = preparation.capture(iter(pending))
    assert [row[:2] for row in rows] == pending
    assert [json.loads(row[2]) for row in rows] == [
        {'library': {'genre': '', 'year': None, 'audio': {}},
         'discovered': {'genre': 'Jazz', 'year': '1999', 'audio': {'bpm': 90}}},
        {'library': {'genre': 'Rock', 'year': '2001', 'audio': {'bpm': 120, 'energy': 0}}, 'discovered': None},
        {'library': None, 'discovered': {'genre': '', 'year': None, 'audio': {}}},
        {'library': None, 'discovered': None},
    ]
    assert all(digest == hashlib.sha256(raw.encode()).hexdigest() and different
               for _key, _revision, raw, digest, different in rows)
    preparation.repository.capture_facts.assert_called_once_with([key for key, _ in pending], mask_numeric=False)


def test_same_digest_is_not_a_meaningful_input_change(preparation):
    row = preparation.capture([('library', 3)])[0]
    preparation.repository.capture_facts.return_value['previous']['library'] = row[3]
    assert preparation.capture([('library', 4)])[0] == ('library', 4, row[2], row[3], False)


def test_numeric_mask_requests_only_metadata_from_both_owners(preparation):
    preparation.repository.capture_facts.return_value['audio'] = {}
    preparation.discovery.discovered_tracks_by_keys.return_value = {'discovery': {'genre': 'Jazz', 'year': '2000'}}
    rows = preparation.capture([('library', 3), ('discovery', 4)], mask_numeric=True)
    preparation.repository.capture_facts.assert_called_once_with(['library', 'discovery'], mask_numeric=True)
    preparation.discovery.discovered_tracks_by_keys.assert_called_once_with(['library', 'discovery'], metadata_only=True)
    assert json.loads(rows[0][2])['library']['audio'] == {}
    assert json.loads(rows[1][2])['discovered']['audio'] == {}


def test_empty_capture_does_not_read_repositories(preparation):
    assert preparation.capture([]) == []
    preparation.repository.capture_reads.assert_not_called()
    preparation.repository.capture_facts.assert_not_called()
    preparation.discovery.discovered_tracks_by_keys.assert_not_called()


@pytest.mark.parametrize('failure', ['notification', 'inputs', 'acknowledgement', None])
def test_publish_orders_independent_writes_and_acknowledges_last(preparation, failure):
    calls = []

    def step(name, result=None):
        def run(*args):
            calls.append(name)
            if failure == name:
                raise RuntimeError(name)
            return result
        return run

    preparation.refresh.enqueue.side_effect = step('notification')
    preparation.repository.publish_capture.side_effect = step('inputs', 1)
    preparation.repository.acknowledge.side_effect = step('acknowledgement')
    rows = preparation.capture([('library', 3)])
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            preparation.publish_capture(rows, 100)
    else:
        assert preparation.publish_capture(rows, 100) == 1
    order = ['notification', 'inputs', 'acknowledgement']
    expected = order[:order.index(failure) + 1] if failure else order
    assert calls == expected
    if 'acknowledgement' in calls:
        preparation.repository.acknowledge.assert_called_once_with([('library', 3)])


def test_unchanged_capture_acknowledges_without_notifying(preparation):
    key, revision, raw, digest, _different = preparation.capture([('library', 3)])[0]
    preparation.publish_capture([(key, revision, raw, digest, False)], 100)
    preparation.refresh.enqueue.assert_not_called()
    preparation.repository.publish_capture.assert_called_once_with([], 100)
    preparation.repository.acknowledge.assert_called_once_with([('library', 3)])


def test_invalid_payload_cannot_notify_write_or_acknowledge(preparation):
    with pytest.raises(ValueError, match='explicit library'):
        preparation.publish_capture([('library', 3, '{}', 'digest', True)], 100)
    preparation.refresh.enqueue.assert_not_called()
    preparation.repository.publish_capture.assert_not_called()
    preparation.repository.acknowledge.assert_not_called()
