"""Clone immutable prepared SQL and every retained file into private Stores."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import importlib
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import pytest

from yt_playlist.core import file_owners, policy_documents, preparation_files, served_facts
from yt_playlist.core.preparation.file_bodies import BODY_OWNERS
from yt_playlist.core.store import Store
from tests.store_fixture_support import PristineSchema, initialize_consumer, revision_definitions


def roots(conn):
    """The current finite file-owner inventory, also for memory fixtures."""
    disk = bool(conn.execute('PRAGMA database_list').fetchone()[2])
    result = {name: module.directory(conn) for name, module in file_owners.owners().items()}
    result['served_facts'] = served_facts.directory(conn)
    for name in BODY_OWNERS:
        if name in {'saved_preparation_files', 'metadata_destination_files'} and not disk:
            continue  # Historical installed preparation requires a disk source.
        module = importlib.import_module('yt_playlist.core.' + name)
        if name in {'sample_files', 'audio_annotation_files'}:
            result[name] = module.directory(conn, memory_key=getattr(conn, '_policy_memory_key', None))
        else:
            result[name] = module.directory(conn)
    if disk:
        for name, directory, _relations in preparation_files.domains():
            result['preparation_' + name] = directory(conn)
    return result


def coordinates(conn, *, logical=()):
    result = []
    for table, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        quoted = '"' + table.replace('"', '""') + '"'
        for column in conn.execute(f'PRAGMA table_info({quoted})'):
            if column[1] == 'filename' or column[1].endswith('_filename'):
                field = '"' + column[1].replace('"', '""') + '"'
                for filename, in conn.execute(f'SELECT DISTINCT {field} FROM {quoted} WHERE {field} IS NOT NULL'):
                    if (table, column[1]) in logical:
                        assert not Path(filename).is_absolute(), (table, column[1], filename)
                        continue
                    result.append((quoted, field, filename))
    return result


def cleanup_memory_files(store, owned):
    if store._db_path is None:
        for root in set(owned.values()):
            store.conn._close_callbacks.append(lambda root=root: shutil.rmtree(root, ignore_errors=True))


@dataclass(frozen=True)
class PreparedTemplate:
    source: Store
    files: dict
    coordinates: tuple
    bundles: dict
    result: object
    preparation_manifest: object = None
    revision_guards: tuple = ()
    revision_tables: frozenset = frozenset()
    revision_views: frozenset = frozenset()

    def restore(self, target, *, ignore=None, external_files=()):
        assert not target.conn.in_transaction
        self.source.conn.backup(target.conn)
        # A backup replaces inputs even if SQLite gives the two copies the same
        # schema epoch. Fence earlier readers and memoized results explicitly.
        target._database_generation = getattr(target, '_database_generation', 0) + 1
        revisions = getattr(target, '_snapshot_revisions', None)
        if (self.revision_guards and revisions is not None
                and revisions.tables == self.revision_tables
                and revisions.views == self.revision_views
                and revision_definitions(target.conn) == self.revision_guards):
            # TEMP survives backup. Reuse guards only when every generated SQL
            # definition and tracked owner matches this immutable source. Their
            # existing counters remain valid because total_changes is monotonic;
            # later writes still stamp the real connection-local triggers.
            revisions.schema_version = target.conn.execute('PRAGMA main.schema_version').fetchone()[0]
        destinations = roots(target.conn)
        cleanup_memory_files(target, destinations)
        relocations = {}
        copied = set()
        for name, source in self.files.items():
            destination = destinations[name]
            relocations[source] = destination
            assert source != destination, name
            if source.exists() and destination not in copied:
                # Byte copies retain FAO logical ages and packed offsets;
                # no hard links or mutable files shared.
                shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
                copied.add(destination)
        external_relocations = {}
        for filename, domain, relative in external_files:
            source = Path(filename)
            destination = destinations[domain] / relative
            assert source != destination
            # Installed recovery may retain a verified file in an earlier
            # compaction tree. Its SQL coordinate must still become private.
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            external_relocations[source] = destination
        target.conn.execute('BEGIN')
        with target.conn:
            with policy_documents.coordinate_updates(target.conn, {table.strip('"') for table, _, _ in self.coordinates}):
                updates = {}
                for table, column, filename in self.coordinates:
                    path = Path(filename)
                    if path in external_relocations:
                        updates.setdefault((table, column), {})[filename] = str(external_relocations[path])
                        continue
                    matches = [(source, destination) for source, destination in relocations.items()
                               if path.is_relative_to(source)]
                    assert matches, 'Unmanaged prepared fixture coordinate: ' + filename
                    source, destination = max(matches, key=lambda pair: len(pair[0].parts))
                    updates.setdefault((table, column), {})[filename] = str(
                        destination / path.relative_to(source))
                for (table, column), mapping in updates.items():
                    items = list(mapping.items())
                    # Bound SQL parameters while avoiding a table scan for
                    # every distinct file in large installed fixture owners.
                    for offset in range(0, len(items), 128):
                        batch = items[offset:offset + 128]
                        cases = ' '.join('WHEN ? THEN ?' for _ in batch)
                        marks = ','.join('?' for _ in batch)
                        target.conn.execute(
                            f'UPDATE {table} SET {column}=CASE {column} {cases} ELSE {column} END '
                            f'WHERE {column} IN ({marks})',
                            (*[value for pair in batch for value in pair],
                             *[filename for filename, _ in batch]))
        if self.preparation_manifest is not None:
            # The owner restores the complete plan/index/unit graph and its
            # digests, preserving opaque originals and FAO logical ages.
            preparation_files.restore(target.conn, self.preparation_manifest)
        target._fixture_bundle_payload = deepcopy(self.bundles)
        return deepcopy(self.result)


def capture_template(source, result, *, logical_coordinates=()):
    """Freeze a genuinely built Store whose connection/files the caller owns.

    Historical and recovery fixtures build through their own transitions rather
    than ordinary current-schema admission. Their consumers use the same private
    file/coordinate restoration as other prepared fixtures.
    """
    assert not source.conn.in_transaction
    preparation = (tuple(preparation_files.references(source.conn))
                   if source._db_path is not None else ())
    manifest = None
    if preparation:
        # Export is read-only SQL work; its retained files live with the owned
        # source database until the higher-scope fixture closes.
        source.conn.execute('BEGIN')
        try:
            manifest = preparation_files.export(source.conn, source._db_path.with_name(
                source._db_path.name + '.fixture-custody'))
        finally:
            source.conn.rollback()
    prepared_columns = {(owner['table'], owner['columns'][0]) for owner in preparation}
    ordinary = tuple((table, column, filename)
        for table, column, filename in coordinates(source.conn, logical=logical_coordinates)
        if (table.strip('"'), column.strip('"')) not in prepared_columns)
    revisions = getattr(source, '_snapshot_revisions', None)
    guards, tables, views = (), frozenset(), frozenset()
    if (revisions is not None and revisions.schema_version ==
            source.conn.execute('PRAGMA main.schema_version').fetchone()[0]):
        guards = revision_definitions(source.conn)
        tables, views = frozenset(revisions.tables), frozenset(revisions.views)
    source.conn.execute('PRAGMA query_only=ON')
    return PreparedTemplate(source, roots(source.conn), ordinary,
        deepcopy(getattr(source, '_fixture_bundle_payload', {})), deepcopy(result), manifest,
        guards, tables, views)


@contextmanager
def historical_template(pristine, seed, *, database, logical_coordinates=()):
    """Freeze a real installed prefix without advancing its historical owners."""
    with ExitStack() as resources:
        home = resources.enter_context(TemporaryDirectory(prefix='tuneconsole-historical-home-'))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', home)
            patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
            patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
            patch.delenv('DEEPSEEK_API_KEY', raising=False)
            source = Store(database)
            resources.callback(source.conn.close)
            pristine.backup(source.conn)
            source._bind_repositories()
            result = seed(source)
            template = capture_template(source, result, logical_coordinates=logical_coordinates)
        yield template


@contextmanager
def prepared_template(pristine, seed, *, database=None, defer_metadata=False):
    """Build with ordinary test settings; retain private files and a read-only template."""
    with ExitStack() as resources:
        home = resources.enter_context(TemporaryDirectory(prefix='tuneconsole-template-home-'))
        # Higher-scope fixtures can run before function isolation or be requested
        # lazily inside it. Restore the caller before yielding, so a later test's
        # teardown cannot undo settings held for the template's whole lifetime.
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', home)
            patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
            patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
            patch.delenv('DEEPSEEK_API_KEY', raising=False)
            # Historical preparation owners require a real database pathname.
            # The higher-scope fixture owns that path through tmp_path_factory.
            source = Store(':memory:' if database is None else database)
            resources.callback(source.conn.close)
            if isinstance(pristine, PristineSchema) and not defer_metadata:
                # The session builder already admitted this exact empty SQL
                # schema. Bind the private consumer just like the store fixture
                # instead of revalidating hundreds of unchanged DDL objects for
                # every independently prepared dataset. Historical/raw sources
                # and explicit deferred-admission checks retain real admission.
                initialize_consumer(source, pristine)
            else:
                pristine.backup(source.conn)
                source.init_schema(defer_metadata=defer_metadata)
            files = roots(source.conn)
            cleanup_memory_files(source, files)
            result = seed(source)
            template = capture_template(source, result)
        yield template
