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
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.preparation.file_bodies import BODY_OWNERS
from yt_playlist.core.store import Store
from tests.store_fixture_support import PristineSchema, initialize_consumer, revision_definitions


CUSTODY_UPGRADE_PHASES = (81, 88, 93, 94, 97, 98, 99, 100, 101, 102, 103, 104, 107)


def custody_predecessor(version, target):
    return max((version, *(step for step in CUSTODY_UPGRADE_PHASES if version < step < target)))


def custody_upgrade_cases(versions):
    return [(version, target, phase) for version in versions
            for target in CUSTODY_UPGRADE_PHASES if target > version
            for phase in ('input', 'converted')]


def assert_upgrade_phase(template, version):
    from yt_playlist.core.migrations import registry

    conn = template.source.conn
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert [tuple(row) for row in conn.execute(
        'SELECT version,name FROM schema_migrations ORDER BY version')] == [
        (item.version, item.name) for item in registry.MIGRATIONS[:version]]
    revisions = getattr(template.source, '_snapshot_revisions', None)
    if revisions is not None:
        assert revisions.conn is conn
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@contextmanager
def custody_templates(tmp_path_factory, pristine):
    templates = {}
    with ExitStack() as resources:
        def get(version, owner, build, *, admit=True, logical_coordinates=(), completed=None):
            builder = getattr(build, 'func', build)
            key = (version, owner, builder.__module__, builder.__qualname__, admit,
                   tuple(logical_coordinates), id(completed))
            if key in templates and getattr(templates[key].source, '_fixture_released', False):
                del templates[key]
            if key not in templates:
                root = tmp_path_factory.mktemp('staged-custody')

                def prepare(source):
                    if admit:
                        source.init_schema(defer_metadata=True)
                    source._bind_repositories()
                    return build(source)

                templates[key] = resources.enter_context(historical_template(
                    completed.source.conn if completed is not None else pristine(version),
                    prepare, database=root / 'source.db',
                    logical_coordinates=logical_coordinates, completed=completed))
            return templates[key]

        yield get


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
    # Filter in SQLite rather than returning every column through the diagnostic
    # cursor and issuing a separate PRAGMA for every unrelated owner.
    fields = conn.execute("SELECT owners.name, fields.name FROM main.sqlite_schema AS owners "
        "CROSS JOIN pragma_table_info(owners.name, 'main') AS fields "
        "WHERE owners.type='table' AND owners.name NOT LIKE 'sqlite_%' "
        "AND (fields.name='filename' OR substr(fields.name,-9)='_filename')").fetchall()
    for table, column in fields:
        quoted = '"' + table.replace('"', '""') + '"'
        field = '"' + column.replace('"', '""') + '"'
        for filename, in conn.execute(f'SELECT DISTINCT {field} FROM main.{quoted} WHERE {field} IS NOT NULL'):
            if (table, column) in logical:
                assert not Path(filename).is_absolute(), (table, column, filename)
                continue
            result.append((quoted, field, filename))
    return result


def schema_definitions(conn):
    return tuple(tuple(row) for row in conn.execute(
        'SELECT type,name,tbl_name,sql FROM main.sqlite_schema ORDER BY type,name'))


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
    external_files: tuple = ()

    @contextmanager
    def open(self, database):
        """Open a private admitted consumer without first cloning an empty DB."""
        assert self.revision_guards, 'Consumer cloning requires an admitted source'
        with ExitStack() as resources:
            home = resources.enter_context(TemporaryDirectory(prefix='tuneconsole-consumer-home-'))
            # Like template construction, cloning can precede function-scoped
            # isolation. Restore caller settings before yielding; retained roots
            # must remain bound to this consumer when those settings change.
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv('YT_PLAYLIST_HOME', home)
                patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
                patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
                patch.delenv('DEEPSEEK_API_KEY', raising=False)
                target = Store(database)
                resources.callback(target.conn.close)
                # Private consumers require real commit visibility and rollback;
                # host power-loss durability is outside this fixture's contract.
                target.conn.execute('PRAGMA synchronous=OFF')
                self.restore(target)
                for name in ('_schema_compatibility', '_rebuild_source_version', '_schema_recovery_pending'):
                    setattr(target, name, getattr(self.source, name))
                target._bind_repositories()
                if target._db_path is not None:
                    mode = target.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
                    assert mode.lower() == 'wal'
            yield target

    def restore(self, target, *, ignore=None, external_files=()):
        external_files = (*self.external_files, *external_files)
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
        elif self.revision_guards:
            # SQL backup excludes TEMP. A prepared source can add real owners;
            # restore its generated guards rather than making the first consumer
            # request rediscover every column and rebuild their SQL in Python.
            existing = {(kind, name): sql for kind, name, sql in revision_definitions(target.conn)}
            desired = {(kind, name): sql for kind, name, sql in self.revision_guards}
            statements = []
            for (kind, name), sql in existing.items():
                if desired.get((kind, name)) != sql:
                    quoted = '"' + name.replace('"', '""') + '"'
                    statements.append(f'DROP {kind.upper()} IF EXISTS temp.{quoted}')
            statements.extend(sql.replace('CREATE ', 'CREATE TEMP ', 1)
                for key, sql in desired.items() if existing.get(key) != sql)
            if statements:
                target.conn.executescript('BEGIN;\n' + ';\n'.join(statements) + ';\nCOMMIT;')
            revisions = Revisions.__new__(Revisions)
            revisions.conn = target.conn
            revisions.schema_version = target.conn.execute('PRAGMA main.schema_version').fetchone()[0]
            revisions.tables = set(self.revision_tables)
            revisions.views = set(self.revision_views)
            target._snapshot_revisions = revisions
        destinations = roots(target.conn)
        cleanup_memory_files(target, destinations)
        relocations = {}
        copied = set()
        for name, source in self.files.items():
            if name not in destinations:
                # Disk-only owners with no payload need no memory namespace.
                assert not any(path.is_file() and path.name != '.readers.lock'
                               for path in source.rglob('*')), name
                continue
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
                    # Walk each ancestry once. Checking every owner with
                    # is_relative_to repeatedly constructs the same parents.
                    source = next((parent for parent in (path, *path.parents)
                                   if parent in relocations), None)
                    assert source is not None, 'Unmanaged prepared fixture coordinate: ' + filename
                    destination = relocations[source]
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
        if (self.revision_guards
                and target._snapshot_revisions.schema_version != target.conn.execute(
                    'PRAGMA main.schema_version').fetchone()[0]
                and schema_definitions(target.conn) == schema_definitions(self.source.conn)
                and revision_definitions(target.conn) == self.revision_guards):
            # Coordinate relocation drops and restores immutable-owner guards.
            # Their exact definitions and all TEMP revision guards still match
            # the admitted source. Keep the real counters rather than rebuilding
            # every table's triggers on the first consumer snapshot. Replacement
            # already fenced older readers through _database_generation above.
            target._snapshot_revisions.schema_version = target.conn.execute(
                'PRAGMA main.schema_version').fetchone()[0]
        target._fixture_bundle_payload = deepcopy(self.bundles)
        return deepcopy(self.result)


def capture_template(source, result, *, logical_coordinates=(), external_files=()):
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
    if revisions is not None:
        revisions.refresh()
        physical = {name for name, in source.conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        actual_views = {name for name, in source.conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='view'")}
        if revisions.tables != physical or revisions.views != actual_views:
            # A backup can give a different schema the same SQLite cookie.
            # Refresh's ordinary cookie fast path cannot establish its owners.
            Revisions.unbind(source.conn)
            revisions = source._snapshot_revisions = Revisions(source.conn)
        # SQLite can retain orphan TEMP trigger catalog rows after backup.
        # DROP IF EXISTS can leave those unreachable rows in the catalog;
        # capture only guards attached to this source's real physical owners.
        attached = {name for name, table in source.conn.execute(
            "SELECT name,tbl_name FROM sqlite_temp_schema WHERE type='trigger' "
            "AND name LIKE 'snapshot_revision_%'") if table in physical}
    if (revisions is not None and revisions.schema_version ==
            source.conn.execute('PRAGMA main.schema_version').fetchone()[0]):
        guards = tuple(row for row in revision_definitions(source.conn)
                       if row[0] == 'table' or row[1] in attached)
        tables, views = frozenset(revisions.tables), frozenset(revisions.views)
    source.conn.execute('PRAGMA query_only=ON')
    return PreparedTemplate(source, roots(source.conn), ordinary,
        deepcopy(getattr(source, '_fixture_bundle_payload', {})), deepcopy(result), manifest,
        guards, tables, views, tuple(external_files))


@contextmanager
def historical_template(pristine, seed, *, database, logical_coordinates=(), schema_replay=False, completed=None):
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
            # Historical fixture inputs keep real commits, recovery files and
            # reopen behavior; host power-loss durability is not their subject.
            source.conn.execute('PRAGMA synchronous=OFF')
            if completed is None:
                pristine.backup(source.conn)
                source._bind_repositories()
                if not schema_replay:
                    source._snapshot_revisions = Revisions(source.conn)
            else:
                assert not schema_replay, 'Historical replay must precede current snapshot guards'
                completed.restore(source)
                for name in ('_schema_compatibility', '_rebuild_source_version', '_schema_recovery_pending'):
                    setattr(source, name, getattr(completed.source, name))
                source._bind_repositories()
            result = seed(source)
            # Explicit migration replay must precede current TEMP guards, which
            # could otherwise retain columns retired by historical DDL. Ordinary
            # seeders keep real snapshot guards available during construction.
            if schema_replay:
                source._snapshot_revisions = Revisions(source.conn)
            cleanup_memory_files(source, roots(source.conn))
            template = capture_template(source, result, logical_coordinates=logical_coordinates)
        yield template


@contextmanager
def prepared_template(pristine, seed, *, database=None, defer_metadata=False, snapshot_guards=True):
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
            if database is not None:
                source.conn.execute('PRAGMA synchronous=OFF')
            if isinstance(pristine, PristineSchema) and not defer_metadata:
                # The session builder supplies exact shipped empty SQL below
                # startup admission. Bind this private consumer like store
                # instead of revalidating hundreds of unchanged DDL objects for
                # every independently prepared dataset. Historical/raw sources
                # and explicit deferred-admission checks retain real admission.
                initialize_consumer(source, pristine, snapshot_guards=snapshot_guards)
            else:
                assert snapshot_guards, 'Raw schema admission owns its snapshot guards'
                pristine.backup(source.conn)
                source.init_schema(defer_metadata=defer_metadata)
            files = roots(source.conn)
            cleanup_memory_files(source, files)
            result = seed(source)
            template = capture_template(source, result)
        yield template
