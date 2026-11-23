"""Bounded maintenance workflows with explicit capabilities and no SQLite."""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
from types import SimpleNamespace

import pytest

from yt_playlist.controllers import preparation
from yt_playlist.controllers.collection_recovery import CollectionRecovery
from yt_playlist.controllers.profile_preparation import ProfilePreparation
from yt_playlist.controllers.embedding_preparation import EmbeddingPreparation
from yt_playlist.core import collection_backup, collection_owners, fao, workflow_settings
from yt_playlist.core.embedding_files import EmbeddingFiles
from yt_playlist.core.schema_contract import MigrationError


def test_collection_restore_checks_current_owners_before_writing():
    table, kind = next(iter(collection_owners.OWNERS.items()))
    record = {'table': table, 'id': 1, 'kind': kind, 'digest': 'a' * 64,
              'bytes': 1, 'relative_filename': 'aa/' + 'a' * 64 + '.json'}
    calls = []
    class Repository:
        @contextmanager
        def restoring(self):
            calls.append('begin')
            try:
                yield
            except ValueError:
                calls.append('rollback')
                raise
        def references(self):
            return [(table, 2, kind, 'changed', record['digest'], 1)]
    files = SimpleNamespace(
        load=lambda _: {'format': collection_backup.FORMAT, 'type': 'manifest', 'chunks': ['b' * 64]},
        chunk=lambda _: {'format': collection_backup.FORMAT, 'type': 'chunk', 'records': [record]},
        validate=lambda _: calls.append('validate'))
    with pytest.raises(ValueError, match='database owners'):
        CollectionRecovery(Repository(), files, relocating=nullcontext).restore('manifest')
    assert calls == ['validate', 'begin', 'rollback']


def test_collection_backup_rejects_escape_paths_before_reading():
    table, kind = next(iter(collection_owners.OWNERS.items()))
    record = {'table': table, 'id': 1, 'kind': kind, 'digest': 'a' * 64,
              'bytes': 1, 'relative_filename': '../outside'}
    with pytest.raises(ValueError, match='owner'):
        collection_backup.add_records({}, {'format': collection_backup.FORMAT, 'type': 'chunk', 'records': [record]})




def test_profile_conversion_wraps_cursor_and_verifies_before_sql_publication():
    calls = []
    row = ('key', 1, '  {"exact": true}\n', 'version', 'digest')
    class Repository:
        def page(self, kind, after, limit):
            calls.append(('page', after))
            return [] if after else [row]

        def publish(self, kind, references):
            calls.append(('publish', references))
    class Files:
        def cursor(self, kind):
            return 'last'

        def describe(self, kind, rows):
            assert rows == [row]
            return ['encoded']

        def write_legacy(self, records):
            assert records == ['encoded']
            calls.append('write')
            return ['receipt']

        def originals(self, kind, references):
            assert references == ['receipt']
            calls.append('verify')
            return [row[2]]

        def advance(self, kind, after):
            calls.append(('cursor', after))
    files = Files()
    command = ProfilePreparation(Repository(), files)
    assert command.prepare('recording', 1) == 1
    assert calls == [('page', 'last'), ('page', ''), 'write', 'verify', ('publish', ['receipt']), ('cursor', 'key')]
    calls.clear()
    files.originals = lambda *_: ['{"exact": true}']
    with pytest.raises(Exception, match='original bytes'):
        command.prepare('recording', 1)
    assert calls == [('page', 'last'), ('page', ''), 'write']


def test_embedding_conversion_preserves_order_basis_and_exact_float_bytes(tmp_path, monkeypatch):
    negative_zero, one = b'\x00\x00\x00\x80', b'\x00\x00\x80\x3f'
    rows = [(9, 41, 'collaborative', 'basis', 1, negative_zero, 123.),
            (10, 12, 'collaborative', 'basis', 1, one, 124.)]
    publications = []
    repository = SimpleNamespace(page=lambda *_: rows, publish=lambda *args: publications.append(args))
    files = EmbeddingFiles(tmp_path)
    command = EmbeddingPreparation(repository, files)
    assert command.prepare('recording', 2) == 2
    (kind, space, basis, references), = publications
    assert (kind, space, basis) == ('recording', 'collaborative', 'basis')
    assert [(subject, width, position, order, built) for subject, width, _, position, order, built in references] == [
        (41, 1, 0, 9, 123.), (12, 1, 1, 10, 124.),
    ]
    heads = [(subject, None, width, digest, position) for subject, width, digest, position, _, _ in references]
    assert files.read_many(kind, heads) == [(41, negative_zero), (12, one)]
    publications.clear()
    monkeypatch.setattr(files, 'read_many', lambda *_: [(41, b'\x00' * 4), (12, one)])
    with pytest.raises(fao.CorruptFile, match='float32 bytes'):
        command.prepare('recording', 2)
    assert publications == []
