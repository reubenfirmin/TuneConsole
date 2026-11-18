"""The backup gate uses detached SQL and file ports, including failures."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playlist_backups import PlaylistBackups

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('failure', [None, 'reserve', 'publish', 'complete'])
def test_capture_returns_only_after_committed_reservation_publication_and_completion(failure):
    events = []
    payload, integrity = {'playlist_id': 7}, ('a' * 64, 32)
    reference = ('/backups/' + 'b' * 32 + '.json', *integrity)

    def step(name):
        events.append(name)
        if failure == name:
            raise OSError('interrupted')

    def reserve(body, expected):
        assert body == payload and expected == integrity
        step('reserve')
        return 'b' * 32

    @contextmanager
    def publish(identity, body, *, expected):
        assert identity == 'b' * 32 and body == payload and expected == integrity
        step('publish')
        try:
            yield reference
        finally:
            events.append('release')

    def complete(identity, actual):
        assert identity == 'b' * 32 and actual == reference
        step('complete')

    repository = SimpleNamespace(reserve=reserve, complete=complete)
    files = SimpleNamespace(describe=Mock(return_value=integrity), write_reserved=publish)
    capture = PlaylistBackups(repository, files)
    if failure:
        with pytest.raises(OSError, match='interrupted'):
            capture.capture(payload)
        expected = {'reserve': ['reserve'], 'publish': ['reserve', 'publish'],
                    'complete': ['reserve', 'publish', 'complete', 'release']}[failure]
    else:
        assert capture.capture(payload) == reference[0]
        expected = ['reserve', 'publish', 'complete', 'release']
    assert events == expected
    files.describe.assert_called_once_with(payload)
