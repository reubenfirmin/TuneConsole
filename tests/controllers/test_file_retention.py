"""Collection admission and failure ordering without SQLite or real files."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.collection_retention import CollectionCollector
from yt_playlist.controllers.managed_file_retention import ManagedFileRetention
from yt_playlist.core.activity import WorkBudgetExceeded

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('admitted,required', [(False, False), (True, True), (True, False)])
def test_managed_file_deletion_requires_admission_and_sql_release(admitted, required):
    events = []

    @contextmanager
    def collecting(root):
        events.append(('fence', root))
        yield admitted
        events.append('unfenced')

    @contextmanager
    def reservation(domain):
        events.append('reserved')
        yield
        events.append('released')

    files = Mock()
    files.candidates.return_value = [('root', 'metadata-runtime', 'path', 'info')]
    files.collecting.side_effect = collecting
    files.owner.side_effect = lambda *args: events.append('inspected') or ('receipt', 7)
    files.remove.side_effect = lambda *args: events.append('deleted') or 1
    repository = Mock()
    repository.reservation.side_effect = reservation
    repository.required.side_effect = lambda *args: events.append('checked') or required
    database = SimpleNamespace(_managed_file_orphans=files)
    owner = ManagedFileRetention(database, repository, (), ())
    assert owner.maintain(100) == int(admitted and not required)
    expected = ['reserved', ('fence', 'root')]
    if admitted:
        expected += ['inspected', 'checked']
        if not required:
            expected += ['deleted']
        repository.required.assert_called_once_with('metadata-runtime', 'path', ('receipt', 7))
    else:
        repository.required.assert_not_called()
    assert events == expected + ['unfenced', 'released']


def test_failed_ownership_check_does_not_delete_and_releases_file_fence():
    released = []

    @contextmanager
    def collecting(root):
        try:
            yield True
        finally:
            released.append(root)

    files = Mock()
    files.candidates.return_value = [('root', 'policies', 'path', 'info')]
    files.collecting.side_effect = collecting
    repository = Mock()
    repository.reservation.side_effect = lambda domain: nullcontext()
    repository.required.side_effect = WorkBudgetExceeded('Interrupted ownership query')
    owner = ManagedFileRetention(SimpleNamespace(_managed_file_orphans=files), repository, (), ())
    with pytest.raises(WorkBudgetExceeded):
        owner.maintain(100)
    files.remove.assert_not_called()
    assert released == ['root']


@pytest.mark.parametrize('active,admitted', [(False, True), (True, False), (True, True)])
def test_collection_batch_remains_until_both_owners_admit_deletion(active, admitted):
    owner = CollectionCollector('unused')
    owner.files.close()
    owner.files = Mock()
    owner.files.batch = ['path']
    owner.files.prepare.return_value = False
    owner.files.collecting.side_effect = lambda: nullcontext(admitted)
    owner.files.remove.return_value = 2
    repository = Mock()
    repository.reservation.side_effect = lambda: nullcontext(active)
    repository.required.return_value = {'retained'}
    assert owner.step(repository) == (2 if active and admitted else 0)
    if active and admitted:
        repository.required.assert_called_once_with(owner.owners, ['path'], owner.OWNER_PAGE)
        owner.files.remove.assert_called_once_with({'retained'})
    else:
        repository.required.assert_not_called()
        owner.files.remove.assert_not_called()
