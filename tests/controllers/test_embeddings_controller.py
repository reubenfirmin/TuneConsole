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
    files.write.return_value = ('prepared',)
    return Embeddings(repo, files)


def test_delta_preserves_existing_order_and_appends_new_subjects(vectors):
    publication = vectors.prepare('rec_vectors', [('a', VECTOR), ('b', VECTOR)], now=10)
    assert publication.model_version == 'existing'
    assert publication.rows == ((1, VECTOR), (2, VECTOR))
    vectors.files.write.assert_called_once_with('recording', 1, [(1, VECTOR, 10, 3), (2, VECTOR, 10, 8)])
    vectors.repository.publish.assert_not_called()


def test_retracted_then_reinserted_subject_gets_new_order(vectors):
    vectors.prepare('rec_vectors', [('a', VECTOR)], ['a'], now=10)
    vectors.files.write.assert_called_once_with('recording', 1, [(1, VECTOR, 10, 8)])


@pytest.mark.parametrize('rows,error', [
    ([('missing', VECTOR)], sqlite3.IntegrityError),
    ([('a', VECTOR), ('alias', VECTOR)], ValueError),
    ([('a', VECTOR), ('b', VECTOR * 2)], ValueError),
    ([('a', struct.pack('f', float('nan')))], ValueError),
    ([('a', b'invalid')], ValueError)])
def test_invalid_owner_or_bytes_never_write_files(vectors, rows, error):
    with pytest.raises(error):
        vectors.prepare('rec_vectors', rows)
    vectors.files.write.assert_not_called()
    vectors.repository.publish.assert_not_called()


def test_replace_writes_all_files_before_publishing_sql(vectors):
    order = []
    vectors.files.write.side_effect = lambda *args: order.append('files') or ('prepared',)
    vectors.repository.publish.side_effect = lambda *args: order.append('sql')
    vectors.replace('rec_vectors', [('a', VECTOR)], {})
    assert order == ['files', 'sql']
    vectors.repository.insertion_orders.assert_not_called()
    assert vectors.repository.publish.call_args.args[0][0].replace


def test_write_failure_never_replaces_selected_vectors(vectors):
    vectors.files.write.side_effect = OSError('full')
    with pytest.raises(OSError):
        vectors.replace('rec_vectors', [('a', VECTOR)], {})
    vectors.repository.publish.assert_not_called()
