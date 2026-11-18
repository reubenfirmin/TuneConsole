"""Vector ownership, validation and file ordering without SQLite."""
from contextlib import nullcontext
import sqlite3
import struct
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.core.embedding_files import EmbeddingFiles
from yt_playlist.repos.embeddings import EmbeddingRepo

pytestmark = pytest.mark.usefixtures('no_database')
VECTOR = struct.pack('f', 1)


@pytest.fixture
def vectors():
    repo = create_autospec(EmbeddingRepo, instance=True, spec_set=True)
    files = create_autospec(EmbeddingFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    repo.head.return_value = ('existing', 1)
    repo.subjects.side_effect = lambda kind, keys, **kwargs: {key: {'a': 1, 'alias': 1, 'b': 2}.get(key) for key in keys}
    repo.insertion_orders.return_value = ({1: 3}, 7)
    files.describe.return_value = [dict(kind='recording', dimensions=1, digest=b'x'*32, size=28)]
    repo.reserve_files.return_value = [17]
    files.write_reserved.return_value = ('prepared',)
    return Embeddings(repo, files)


def test_delta_preserves_existing_order_and_appends_new_subjects(vectors):
    publication = vectors.prepare('rec_vectors', [('a', VECTOR), ('b', VECTOR)], now=10)
    assert publication.model_version == 'existing'
    assert publication.rows == ((1, VECTOR), (2, VECTOR))
    vectors.files.describe.assert_called_once_with('recording', 1, [(1, VECTOR, 10, 3), (2, VECTOR, 10, 8)])
    vectors.repository.reserve_files.assert_called_once_with(vectors.files.describe.return_value)
    assert vectors.files.write_reserved.call_args.args[0][0]['file_id'] == 17
    vectors.repository.publish.assert_not_called()


def test_retracted_then_reinserted_subject_gets_new_order(vectors):
    vectors.prepare('rec_vectors', [('a', VECTOR)], ['a'], now=10)
    vectors.files.describe.assert_called_once_with('recording', 1, [(1, VECTOR, 10, 8)])


@pytest.mark.parametrize('rows,error', [
    ([('missing', VECTOR)], sqlite3.IntegrityError),
    ([('a', VECTOR), ('alias', VECTOR)], ValueError),
    ([('a', VECTOR), ('b', VECTOR * 2)], ValueError),
    ([('a', struct.pack('f', float('nan')))], ValueError),
    ([('a', b'invalid')], ValueError)])
def test_invalid_owner_or_bytes_never_write_files(vectors, rows, error):
    with pytest.raises(error):
        vectors.prepare('rec_vectors', rows)
    vectors.files.describe.assert_not_called()
    vectors.repository.reserve_files.assert_not_called()
    vectors.files.write_reserved.assert_not_called()
    vectors.repository.publish.assert_not_called()


def test_replace_writes_all_files_before_publishing_sql(vectors):
    order = []
    vectors.repository.reserve_files.side_effect = lambda *args: order.append('pending') or [17]
    vectors.files.write_reserved.side_effect = lambda *args: order.append('files') or ('prepared',)
    vectors.repository.publish.side_effect = lambda *args: order.append('sql')
    vectors.replace('rec_vectors', [('a', VECTOR)], {})
    assert order == ['pending', 'files', 'sql']
    vectors.repository.insertion_orders.assert_not_called()
    assert vectors.repository.publish.call_args.args[0][0].replace


def test_write_failure_never_replaces_selected_vectors(vectors):
    vectors.files.write_reserved.side_effect = OSError('full')
    with pytest.raises(OSError):
        vectors.replace('rec_vectors', [('a', VECTOR)], {})
    vectors.repository.publish.assert_not_called()


def test_snapshot_preparation_reserves_on_the_writer_but_reads_its_pinned_repository(vectors):
    writer = create_autospec(EmbeddingRepo, instance=True, spec_set=True)
    writer.reserve_files.return_value = [18]
    vectors.file_repository = writer
    vectors.prepare('rec_vectors', [('a', VECTOR)], now=10)
    writer.reserve_files.assert_called_once_with(vectors.files.describe.return_value)
    writer.head.assert_not_called()
    vectors.repository.reserve_files.assert_not_called()
    assert vectors.files.write_reserved.call_args.args[0][0]['file_id'] == 18


def test_collection_checks_each_sql_owner_without_a_writer_transaction(vectors):
    candidates = [('recording', 17, 'held', 'info'), ('artist', b'x'*32, 'orphan', 'other')]
    vectors.files.garbage.side_effect = lambda *a, **k: nullcontext(iter(candidates))
    vectors.repository.file_required.side_effect = [True, False]
    vectors.files.remove.return_value = True
    assert vectors.collect(10000000, limit=2) == 1
    vectors.files.remove.assert_called_once_with('orphan', 'other')
