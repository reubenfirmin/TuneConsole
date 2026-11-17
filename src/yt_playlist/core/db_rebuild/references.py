"""SQLite reference and reader-root discovery for a write-fenced rebuild.

No file inspection, opening, locking or custody changes occur here. Optional
installed references retain that fact until the file capability inspects them.
"""
from dataclasses import dataclass
from pathlib import Path

from .. import (choice_evidence_files, file_owners, metadata_runtime,
               metadata_runtime_files, served_facts)
from .sqlite_checks import tables


@dataclass(frozen=True)
class Reference:
    path: Path
    required: bool = True


@dataclass(frozen=True)
class MapReference:
    root: Path
    publication: str | None


@dataclass(frozen=True)
class EvidenceReferences:
    root: Path
    owned: frozenset[int]


def foreign_keys(conn):
    from .retention_plan import quote
    schema = {}
    for table in tables(conn):
        groups = {}
        for row in conn.execute('PRAGMA main.foreign_key_list(' + quote(table) + ')'):
            groups.setdefault((row[0], row[2]), []).append((row[3], row[4]))
        schema[table] = tuple((parent, tuple(columns)) for (_, parent), columns in groups.items())
    return schema


def owners(conn):
    for name, owner in file_owners.owners().items():
        if owner.available(conn):
            if name == 'legacy-journals':
                selected = [MapReference(owner.directory(conn) / domain, publication.hex())
                    for domain, publication in conn.execute('SELECT domain,publication FROM legacy_archive_heads ORDER BY domain')]
            elif name == 'policy-history':
                selected = [MapReference(owner.directory(conn), owner.head(conn))]
                if 'policy_history_files' in tables(conn):
                    from ..policy_paths import record_path
                    selected.extend(Reference(record_path(owner.directory(conn), identity)) for identity, in conn.execute(
                        'SELECT f.id FROM policy_history_files f JOIN recommendation_policy_history h '
                        "ON f.version=h.version WHERE f.file_status='ready' AND h.state='archived'"))
            else:
                selected = (Reference(Path(path)) for path in owner.references(conn))
            yield name, owner, selected
    from yt_playlist.repos import evidence_file_writes
    owned = frozenset(row[0] for row in evidence_file_writes.references(conn).values())
    yield 'choice_evidence', choice_evidence_files, (EvidenceReferences(choice_evidence_files.directory(conn), owned),)
    if served_facts.available(conn):
        root = served_facts.directory(conn)
        yield 'served', served_facts, (Reference(root / served_facts.relative(row['id']))
                                      for row in served_facts.references(conn))
    if metadata_runtime.active(conn):
        yield 'runtime_metadata', metadata_runtime_files, (
            Reference(metadata_runtime_files.reference_path(conn, kind, owner, revision), required)
            for kind, owner, revision, required in metadata_runtime_files.references(conn))


def inventory(conn):
    """Explicit owner references plus installed absolute SQL locators."""
    from .retention_plan import quote
    for _, _, references in owners(conn):
        yield from references
    for table in tables(conn):
        declared = [row[1] for row in conn.execute('PRAGMA main.table_info(' + quote(table) + ')')]
        columns = [name for name in declared if (name == 'filename' or name.endswith('_filename'))
                   and (table, name) != ('action_backups', 'filename')]
        ready = " AND file_status='ready'" if table == 'sample_attributes' and 'file_status' in declared else ''
        for column in columns:
            for row in conn.execute('SELECT DISTINCT ' + quote(column) + ' FROM ' + quote(table)
                    + ' WHERE ' + quote(column) + ' IS NOT NULL' + ready):
                path = Path(row[0])
                if path.is_absolute():
                    yield Reference(path)


def reader_roots(conn):
    from .. import (collection_recovery, group_documents, legacy_evidence, metadata_delivery_files,
                   policy_documents, recommendation_outputs, sample_files, file_paths)
    source_tables = tables(conn)
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    memory_key = getattr(conn, '_policy_memory_key', id(conn))
    yield from (file_paths.root_for_database(database, memory_key, domain) for domain in file_owners.owners())
    yield choice_evidence_files.directory(conn)
    if served_facts.available(conn):
        yield served_facts.directory(conn)
    if 'metadata_runtime_state' in source_tables:
        yield metadata_runtime_files.directory(conn)
    for table, module in (('sample_attributes', sample_files), ('album_feedback', collection_recovery),
            ('recommendation_policies', policy_documents), ('group_versions', group_documents),
            ('prepared_outputs', recommendation_outputs), ('metadata_deliveries', metadata_delivery_files),
            ('legacy_catalog_evidence', legacy_evidence)):
        if table in source_tables:
            yield module.directory(conn)
