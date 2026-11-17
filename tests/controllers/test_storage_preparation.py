"""Bounded maintenance workflows with explicit capabilities and no SQLite."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from types import SimpleNamespace

import pytest

from yt_playlist.controllers import preparation
from yt_playlist.controllers.collection_recovery import CollectionRecovery
from yt_playlist.controllers.profile_preparation import ProfilePreparation
from yt_playlist.controllers.embedding_preparation import EmbeddingPreparation
from yt_playlist.core import collection_backup, collection_owners, workflow_settings
from yt_playlist.core.db_rebuild import retention_plan
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
        CollectionRecovery(Repository(), files).restore('manifest')
    assert calls == ['validate', 'begin', 'rollback']


def test_collection_backup_rejects_escape_paths_before_reading():
    table, kind = next(iter(collection_owners.OWNERS.items()))
    record = {'table': table, 'id': 1, 'kind': kind, 'digest': 'a' * 64,
              'bytes': 1, 'relative_filename': '../outside'}
    with pytest.raises(ValueError, match='owner'):
        collection_backup.add_records({}, {'format': collection_backup.FORMAT, 'type': 'chunk', 'records': [record]})


def test_retention_plan_protects_unknown_fk_owners_and_rejects_composite_references():
    schema = {name: () for name in ('recommendation_generations', 'recommendation_heads',
        'recommendation_generation_refs', 'recommendation_builds')}
    schema['future_owner'] = (('recommendation_generations', (('generation', 'id'),)),)
    selected = retention_plan.plan(schema, now=123)
    assert any('"future_owner"' in sql and 'r."generation"=compact_drop_generations.id' in sql
               for sql, _ in selected.statements)
    assert any(parameters == (123,) for _, parameters in selected.statements)
    assert 'future_owner' not in selected.predicates
    schema['future_owner'] = (('recommendation_generations', (('a', 'id'), ('b', 'version'))),)
    with pytest.raises(ValueError, match='Unsupported recommendation generation reference'):
        retention_plan.plan(schema, now=123)


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


def test_embedding_conversion_preserves_order_basis_and_exact_float_bytes():
    vector = b'\x00\x00\x00\x80'  # Float32 negative zero must survive exactly.
    calls = []
    row = (9, 41, 'collaborative', 'basis', 1, vector, 123.)
    reference = (41, 1, b'digest', 0, 9, 123.)
    repository = SimpleNamespace(page=lambda *_: [row],
        publish=lambda *args: calls.append(('publish', args)))
    files = SimpleNamespace(write_legacy=lambda *args: calls.append(('write', args)) or [reference],
        read_many=lambda *_: [(41, vector)])
    command = EmbeddingPreparation(repository, files)
    assert command.prepare('recording', 1) == 1
    assert calls == [('write', ('recording', 1, [(41, vector, 123., 9)])),
                     ('publish', ('recording', 'collaborative', 'basis', [reference]))]
    calls.clear()
    files.read_many = lambda *_: [(41, b'\x00' * 4)]
    with pytest.raises(Exception, match='float32 bytes'):
        command.prepare('recording', 1)
    assert [call[0] for call in calls] == ['write']
