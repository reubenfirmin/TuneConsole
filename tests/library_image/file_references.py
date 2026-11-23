"""Discover files and reader roots for private test libraries."""
from dataclasses import dataclass
from pathlib import Path
import time

from yt_playlist.core import (choice_evidence_files, file_owners, legacy_journals, metadata_file_contract, metadata_runtime,
                              metadata_runtime_files, paid_description_access, runtime_metadata_choices, served_facts)
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.core.migrations.control_settings_storage import predecessor_metadata_active
from yt_playlist.core.policy_paths import record_path


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


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def description_access_in_provider_row(conn):
    return conn.execute("SELECT 1 FROM pragma_table_info('enrichment_provider_config') "
                        "WHERE name='description_access'").fetchone() is not None


def description_access_receipts(conn, *, provider_row):
    if provider_row:
        return paid_description_access.receipts(conn)
    if 'settings' not in tables(conn):
        return ()
    # The capture tool also inventories installed libraries before upgrading them.
    return conn.execute("""
        SELECT key,json_extract(value,'$.evidence_filename'),json_extract(value,'$.evidence_digest')
        FROM settings WHERE key GLOB ? AND json_extract(value,'$.evidence_filename') IS NOT NULL
        ORDER BY key
    """, (paid_description_access.BACKUP_KEY_PREFIX + '*',))


def setting_file_references(conn):
    from yt_playlist.core import collection_owners, model_state, policy_bodies, recommendation_outputs, spotify_records

    if 'settings' not in tables(conn):
        return
    for kind, reference in model_state.references(conn):
        yield model_state.KEYS[kind], '$.recovery_filename', Path(reference[0])
    for owner, (key, _) in recommendation_outputs.SAVED_OUTPUTS.items():
        reference = recommendation_outputs.saved_reference(conn, owner)
        if reference is not None:
            yield key, '$.filename', Path(reference['filename'])
    from yt_playlist.core.sync_state import ATTEMPT
    for key, filename in conn.execute("SELECT key,value->>'recovery_filename' FROM settings WHERE key IN (?,?,?)",
                                      (*collection_owners.TASTE_CONTROLS.values(), ATTEMPT)):
        if filename is not None:
            yield key, '$.recovery_filename', Path(filename)
    for prefix in spotify_records.ARCHIVE_PREFIXES.values():
        for key, _, filename, _, _ in spotify_records.references(conn, prefix):
            if filename is not None:
                yield key, '$.recovery_filename', Path(filename)
    for key, filename in conn.execute("SELECT key,value->>'$[0]' FROM settings WHERE key GLOB ?",
                                      (policy_bodies.PATTERN,)):
        yield key, '$[0]', Path(filename)


def publication_file_references(conn):
    from yt_playlist.core import publication_files, recommendation_outputs

    for table in publication_files.PREFIXES:
        if publication_files.installed_table(conn, table):
            if table == publication_files.REPORT_TABLE:
                if conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (recommendation_outputs.REPORT_PARTS,)).fetchone():
                    for identity, filename in conn.execute('SELECT id,filename FROM recommendation_report_parts'):
                        yield table, identity, Path(filename)
                continue
            if not conn.execute("SELECT 1 FROM pragma_table_info(?) WHERE name='files'", (table,)).fetchone():
                continue
        if 'settings' in tables(conn):
            for identity, _, _, filename, _, _ in publication_files.references(conn, table):
                yield table, identity, Path(filename)


def _runtime_revisions(conn, source_tables):
    if 'metadata_runtime_providers' not in source_tables:
        yield from metadata_runtime_files.active_references(conn, choice_relation=runtime_metadata_choices.relation(conn))
        return
    # Capture the installed layout before applying pending migrations in a workspace.
    for recording, provider, at in conn.execute(
            'SELECT s.recording_id,p.id,s.at FROM enrichment_provider_state s '
            "JOIN metadata_runtime_providers p ON p.name=s.provider WHERE s.at>? AND s.status<>'queued'",
            (time.time() - ALTERNATIVE_SECONDS,)):
        yield 'alternatives', (recording, provider), float(at), False
    for key, revision in conn.execute('SELECT source_key,revision FROM rec_musical_sources'):
        yield 'native', (key,), revision, True
    for identity, revision in conn.execute('SELECT id,revision FROM metadata_runtime_choices'):
        for number in range(1, revision + 1):
            yield 'choices', (identity,), number, True
    for identity, revision in conn.execute('SELECT id,revision FROM metadata_runtime_conflicts'):
        yield 'conflicts', (identity,), revision, True


def installed_policy_history(conn):
    from yt_playlist.core import policy_history_read as history

    if history.available(conn):
        return history.publication(conn), list(history.archive_ids(conn))
    columns = {row[1] for row in conn.execute("PRAGMA table_info('recommendation_policy_history')")}
    if 'archive_file_id' not in columns:
        return None, []
    if 'shared_archive_head' in columns:
        head = conn.execute('SELECT shared_archive_head FROM recommendation_policy_history '
                            'WHERE shared_archive_head IS NOT NULL LIMIT 1').fetchone()
    else:
        head = conn.execute('SELECT value FROM settings WHERE key=?', (history.ARCHIVE_HEAD,)).fetchone()
    identities = [row[0] for row in conn.execute('SELECT archive_file_id FROM recommendation_policy_history '
                                               'WHERE archive_file_id IS NOT NULL')]
    return head[0] if head else None, identities


def owners(conn):
    source_tables = tables(conn)
    shared_receipts = conn.execute("SELECT 1 FROM pragma_table_xinfo('collection_file_writes') "
                                   "WHERE name='file_address'").fetchone() is not None
    publications = list(legacy_journals.publications(conn)) if 'settings' in source_tables else []
    if publications:
        yield 'legacy-journals', legacy_journals, (
            MapReference(legacy_journals.directory(conn) / domain, publication)
            for domain, publication in publications)
    for name, owner in file_owners.owners().items():
        if name == 'policy-history':
            root = owner.directory(conn)
            if 'policy_history_archive' in source_tables:
                row = conn.execute('SELECT publication FROM policy_history_archive WHERE id=1').fetchone()
                selected = [MapReference(root, row[0].hex() if row else None)]
                if 'policy_history_files' in source_tables:
                    selected.extend(Reference(record_path(root, identity)) for identity, in conn.execute(
                        'SELECT f.id FROM policy_history_files f JOIN recommendation_policy_history h '
                        "ON f.version=h.version WHERE f.file_status='ready' AND h.state='archived'"))
                yield name, owner, selected
            elif owner.available(conn) or 'recommendation_policy_history' in source_tables:
                head, identities = installed_policy_history(conn)
                yield name, owner, [MapReference(root, head),
                                    *(Reference(record_path(root, identity)) for identity in identities)]
        elif owner.available(conn):
            yield name, owner, (Reference(Path(path)) for path in owner.references(conn))
    evidence_sql = None
    if 'evidence_file_references' in source_tables:
        evidence_sql = "SELECT r.file_id FROM evidence_file_references r JOIN evidence_file_writes w ON w.id=r.file_id WHERE w.file_status='ready'"
    elif 'evidence_file_writes' in source_tables:
        evidence_sql = 'SELECT id FROM evidence_file_writes WHERE selected=1'
    elif shared_receipts:
        evidence_sql = "SELECT file_address FROM collection_file_writes WHERE kind='listening-evidence' AND selected=1"
    owned = frozenset(row[0] for row in conn.execute(evidence_sql)) if evidence_sql else frozenset()
    yield 'choice_evidence', choice_evidence_files, (EvidenceReferences(choice_evidence_files.directory(conn), owned),)
    if served_facts.available(conn):
        root = served_facts.directory(conn)
        yield 'served', served_facts, (Reference(root / served_facts.relative(row['id']))
                                      for row in served_facts.references(conn))
    if (('metadata_runtime_choices' in source_tables or runtime_metadata_choices.relation(conn) == runtime_metadata_choices.VIEW)
            and (predecessor_metadata_active(conn) if 'metadata_runtime_state' in source_tables
                 else metadata_runtime.active(conn))):
        revisions = _runtime_revisions(conn, source_tables)
        if 'metadata_file_references' in source_tables:
            root = metadata_runtime_files.directory(conn)
            def installed_metadata_references():
                for kind, owner, revision, required in revisions:
                    row = conn.execute('SELECT file_id FROM metadata_file_references '
                        'WHERE kind=? AND owner_key=? AND revision_key=?',
                        (kind, metadata_file_contract.owner_key(kind, owner),
                         metadata_file_contract.revision_key(revision))).fetchone()
                    path = (metadata_file_contract.record_path(root, row[0]) if row else
                            root / metadata_runtime_files.relative(kind, owner, revision))
                    yield Reference(path, required)
            selected = installed_metadata_references()
        elif 'metadata_file_writes' in source_tables:
            root = metadata_runtime_files.directory(conn)
            def installed_selected_metadata():
                for kind, owner, revision, required in revisions:
                    row = conn.execute('SELECT id FROM metadata_file_writes WHERE '
                        'kind=? AND owner_key=? AND revision_key=? AND selected=1',
                        (kind, metadata_file_contract.owner_key(kind, owner),
                         metadata_file_contract.revision_key(revision))).fetchone()
                    path = (metadata_file_contract.record_path(root, row[0]) if row else
                            root / metadata_runtime_files.relative(kind, owner, revision))
                    yield Reference(path, required)
            selected = installed_selected_metadata()
        elif shared_receipts:
            selected = (Reference(metadata_runtime_files.reference_path(conn, kind, owner, revision), required)
                        for kind, owner, revision, required in revisions)
        else:
            root = metadata_runtime_files.directory(conn)
            selected = (Reference(root / metadata_runtime_files.relative(kind, owner, revision), required)
                        for kind, owner, revision, required in revisions)
        yield 'runtime_metadata', metadata_runtime_files, selected


def action_backup_references(conn):
    if any(row[1] == 'backup_inventory' for row in conn.execute('PRAGMA main.table_info(actions)')):
        for action, position, filename in conn.execute("""
                SELECT a.id, item.value->>'position', item.value->>'retained_filename'
                FROM actions a JOIN json_each(a.backup_inventory) item
                WHERE item.value->>'retained_filename' IS NOT NULL"""):
            yield action, position, Path(filename)


def inventory(conn):
    """Explicit owner references plus installed absolute SQL locators."""
    for _, _, references in owners(conn):
        yield from references
    source_tables = tables(conn)
    for _, filename, _ in description_access_receipts(conn, provider_row=description_access_in_provider_row(conn)):
        if (path := Path(filename)).is_absolute():
            yield Reference(path)
    for _, _, path in setting_file_references(conn):
        if path.is_absolute():
            yield Reference(path)
    for _, _, path in publication_file_references(conn):
        if path.is_absolute():
            yield Reference(path)
    for _, _, path in action_backup_references(conn):
        if path.is_absolute():
            yield Reference(path)
    for table in source_tables:
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
    from yt_playlist.core import (collection_recovery, group_documents, legacy_evidence, metadata_delivery_files,
                  policy_documents, recommendation_chunks, recommendation_outputs, sample_files, file_paths)
    source_tables = tables(conn)
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    memory_key = getattr(conn, '_policy_memory_key', id(conn))
    yield from (file_paths.root_for_database(database, memory_key, domain) for domain in file_owners.owners())
    if 'legacy_archive_heads' in source_tables:
        yield legacy_journals.directory(conn)
    yield choice_evidence_files.directory(conn)
    if served_facts.available(conn):
        yield served_facts.directory(conn)
    if metadata_runtime.installed(conn) or runtime_metadata_choices.relation(conn) == runtime_metadata_choices.VIEW:
        yield metadata_runtime_files.directory(conn)
    for table, module in (('sample_attributes', sample_files), ('album_feedback', collection_recovery),
            ('recommendation_policies', policy_documents), ('group_versions', group_documents),
            (recommendation_chunks.OWNER, recommendation_outputs), ('metadata_deliveries', metadata_delivery_files)):
        if table in source_tables:
            yield module.directory(conn)
    if recommendation_outputs._has_relation(conn, recommendation_chunks.VIEW):
        yield recommendation_outputs.directory(conn)
    if source_tables.intersection(legacy_evidence.DOMAINS.values()):
        yield legacy_evidence.directory(conn)
