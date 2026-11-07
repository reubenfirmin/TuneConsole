"""Recover the explicit installed preparation owners and their durable plans.

Raw originals and recovery evidence remain byte exact. Only file coordinates in
generated plan/index/unit documents are rebuilt for the disposable destination.
The owner's own schema supplies keys; this is not a global artifact registry.
"""
from functools import partial
import hashlib
from pathlib import Path

from . import policy_documents, recommendation_outputs as files, fao
from .activity import check_cancellable_work


def domains():
    from .preparation import (
        action_ledger, album_notes, album_shares, append_operations, choice_learning,
        collection_documents, collection_feedback, discovery_artists, genre_preferences, home_outputs,
        library_sync, listening_outputs, metadata_conflicts, model_artifacts,
        playback_context, radio_origins, radio_session, ranked_state,
        recipe_memberships, recommendation_reports, road_trip_drafts,
        taste_controls, tv_state, workflow_receipts)

    for module in (action_ledger, album_notes, album_shares, choice_learning,
                   home_outputs, library_sync, listening_outputs, metadata_conflicts,
                   model_artifacts, radio_origins, radio_session, recipe_memberships,
                   recommendation_reports, road_trip_drafts):
        yield module.__name__.rsplit('.', 1)[1], module.directory, module.DEFINITION.destinations
    yield 'genre_preferences', genre_preferences.directory, genre_preferences.DEFAULT_DEFINITION.destinations
    for module in (collection_documents, discovery_artists, ranked_state,
                   taste_controls, tv_state, workflow_receipts):
        definitions = tuple(pair for job in module.JOBS.values() for pair in job.definition.destinations)
        yield module.__name__.rsplit('.', 1)[1], module.directory, definitions
    for module in (collection_feedback, playback_context):
        definitions = tuple(pair for source in module.PREFIXES for pair in module.definitions(source))
        yield module.__name__.rsplit('.', 1)[1], module.directory, definitions
    for name, job in append_operations.JOBS.items():
        yield 'append_' + name, partial(append_operations.directory, name=name), job.definition.destinations


def _owners(conn):
    """Validate only explicitly declared file tables, including retired owners."""
    from .schema_contract import tokens as _tokens
    seen = set()
    for domain, directory, definitions in domains():
        for table, definition in definitions:
            if table in seen or not definition.lstrip().upper().startswith('CREATE TABLE'):
                continue
            seen.add(table)
            columns = conn.execute('PRAGMA main.table_info(' + table + ')').fetchall()
            if not columns:
                continue
            names = {column[1] for column in columns}
            slots = []
            for filename, digest, size in (
                    ('filename', 'content_digest' if 'content_digest' in names else 'digest', 'byte_size'),
                    ('plan_filename', 'plan_digest', 'plan_bytes'),
                    ('recovery_filename', 'recovery_digest', 'recovery_bytes'),
                    ('retained_filename', 'backup_digest', 'backup_bytes')):
                if filename in names:
                    if not {digest, size} <= names:
                        raise ValueError('Unsupported preparation file coordinates: ' + table)
                    slots.append((filename, digest, size))
            if not slots:
                continue
            # v103 renames the native account/scope owners, not these retained
            # preparation tables. Their captured scope_ref fields and CHECKs
            # keep the original definition through that supported upgrade.
            actual = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (table,)).fetchone()[0]
            if _tokens(actual) != _tokens(definition):
                raise ValueError('Incompatible preparation file owner: ' + table)
            keys = tuple(column[1] for column in sorted(columns, key=lambda c: c[5]) if column[5])
            if not keys:
                raise ValueError('Preparation file owner requires a durable key: ' + table)
            yield domain, directory, table, keys, slots


def references(conn):
    for domain, directory, table, keys, slots in _owners(conn):
        for slot in slots:
            query = 'SELECT ' + ','.join((*keys, *slot)) + ' FROM ' + table
            query += ' WHERE ' + slot[0] + ' IS NOT NULL ORDER BY ' + ','.join(keys)
            for row in conn.execute(query):
                yield {'domain': domain, 'table': table, 'keys': dict(zip(keys, row[:len(keys)])),
                       'columns': list(slot), 'reference': list(row[len(keys):])}


def directories(conn):
    owners = {owner[0] for owner in _owners(conn)}
    return [directory(conn) for domain, directory, _ in domains() if domain in owners]


def available(conn):
    return next(_owners(conn), None) is not None


def _read(reference, *, with_age=False):
    filename, digest, size = reference
    # The largest retained original is the bounded album-note source (512 MiB).
    # Read one file at a time; never concatenate an owner's whole history.
    if (not isinstance(filename, str) or not files._digest(digest)
            or type(size) is not int or not 0 < size <= 512 * 1024 * 1024):
        raise ValueError('Invalid preparation file reference')
    path = Path(filename)
    with fao.read(path) as stream:
        encoded = stream.read(size + 1)
        age = stream.info
    if path.stem != digest or len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('Preparation file differs from retained bytes')
    return (encoded, age) if with_age else encoded


def _coordinates(value, visit):
    """Walk typed plan coordinates; opaque raw text is never interpreted."""
    if isinstance(value, dict):
        result = {key: _coordinates(item, visit) for key, item in value.items()}
        for filename, digest, size in (('filename', 'digest', 'byte_size'),
                ('filename', 'content_digest', 'byte_size'),
                ('plan_filename', 'plan_digest', 'plan_bytes'),
                ('recovery_filename', 'recovery_digest', 'recovery_bytes'),
                ('retained_filename', 'backup_digest', 'backup_bytes')):
            if result.get(filename) is not None and all(key in result for key in (filename, digest, size)):
                result[filename], result[digest], result[size] = visit([result[filename], result[digest], result[size]])
        return result
    if isinstance(value, list):
        result, position = [], 0
        while position < len(value):
            if (position + 2 < len(value) and isinstance(value[position], str)
                    and Path(value[position]).is_absolute() and files._digest(value[position + 1])
                    and type(value[position + 2]) is int):
                result.extend(visit(value[position:position + 3]))
                position += 3
            else:
                result.append(_coordinates(value[position], visit))
                position += 1
        return result
    return value


def _graph(encoded, visit):
    # Only generated plan pages contain custody coordinates. Original output,
    # raw text and native recovery envelopes retain their exact original bytes.
    try:
        document = files._decode(encoded)
    except ValueError:
        return encoded
    if document['chunk_key'] not in {'plan', 'index', 'units'}:
        return encoded
    return files.encode(document['surface'], document['chunk_key'], _coordinates(document['output'], visit))


def export(conn, root):
    if not conn.in_transaction:
        raise ValueError('Preparation export requires a pinned database snapshot')
    root = Path(root)
    if any(root.resolve() == directory.resolve() for directory in directories(conn)):
        raise ValueError('Preparation backup requires a separate directory')
    records, chunks, captured = [], [], set()

    def record(value):
        records.append(value)
        if len(records) == 128:
            chunks.append(Path(files._manifest(root, {'type': 'preparation-chunk', 'records': records})).stem)
            records.clear()

    def retain(domain, reference, graph=False):
        identity = (domain, *reference)
        if identity in captured:
            return reference
        check_cancellable_work()
        captured.add(identity)
        encoded, age = _read(reference, with_age=True)
        if graph:
            # Traversal exports every page and nested recovery/backup, including
            # units which a partially processed generation has not copied to SQL.
            _graph(encoded, lambda child: retain(domain, child, True))
        copied = policy_documents._publish_encoded(root, encoded, reference[1], age=age)
        record({'type': 'file', 'domain': domain, 'reference': reference,
                'relative_filename': str(Path(copied).relative_to(root)), 'graph': graph})
        return reference

    for owner in references(conn):
        retain(owner['domain'], owner['reference'], owner['columns'][0] == 'plan_filename')
        record({'type': 'owner', **owner})
    if records:
        chunks.append(Path(files._manifest(root, {'type': 'preparation-chunk', 'records': records})).stem)
    return files._manifest(root, {'type': 'preparation-manifest', 'chunks': chunks})


def _identity(owner):
    return owner['table'], tuple(sorted(owner['keys'].items())), tuple(owner['columns'])


def restore(conn, manifest):
    if conn.in_transaction:
        raise ValueError('Preparation restore requires an idle connection')
    path = Path(manifest)
    root, value = path.parent.parent, files._load_manifest(path)
    if (set(value) != {'format', 'type', 'chunks'} or value['type'] != 'preparation-manifest'
            or not isinstance(value['chunks'], list) or any(not files._digest(d) for d in value['chunks'])
            or len(set(value['chunks'])) != len(value['chunks'])):
        raise ValueError('Invalid preparation backup manifest')
    domains_by_name = {name: directory for name, directory, _ in domains()}
    owners, retained = {}, {}
    for digest in value['chunks']:
        chunk = files._load_manifest(root / digest[:2] / (digest + '.json'))
        if (set(chunk) != {'format', 'type', 'records'} or chunk['type'] != 'preparation-chunk'
                or not isinstance(chunk['records'], list) or not 1 <= len(chunk['records']) <= 128):
            raise ValueError('Invalid preparation backup chunk')
        for entry in chunk['records']:
            if not isinstance(entry, dict) or entry.get('domain') not in domains_by_name:
                raise ValueError('Invalid preparation backup domain')
            if entry.get('type') == 'owner':
                if set(entry) != {'type', 'domain', 'table', 'keys', 'columns', 'reference'}:
                    raise ValueError('Invalid preparation backup owner')
                identity = _identity(entry)
                if identity in owners:
                    raise ValueError('Duplicate preparation backup owner')
                owners[identity] = {key: item for key, item in entry.items() if key != 'type'}
            elif entry.get('type') == 'file':
                if set(entry) != {'type', 'domain', 'reference', 'relative_filename', 'graph'} or type(entry['graph']) is not bool:
                    raise ValueError('Invalid preparation backup file')
                reference = entry['reference']
                if not isinstance(reference, list) or len(reference) != 3 or not files._digest(reference[1]):
                    raise ValueError('Invalid preparation backup reference')
                relative = reference[1][:2] + '/' + reference[1] + '.json'
                identity = (entry['domain'], *reference)
                if entry['relative_filename'] != relative or identity in retained:
                    raise ValueError('Invalid or duplicate preparation backup file')
                _read([str(root / relative), *reference[1:]])
                retained[identity] = entry
            else:
                raise ValueError('Unsupported preparation backup record')

    conn.execute('BEGIN IMMEDIATE')
    try:
        actual = {_identity(owner): owner for owner in references(conn)}
        if actual != owners:
            raise ValueError('Preparation backup differs from database custody')
        installed, visiting = {}, set()

        def install(domain, reference, depth=0):
            identity = (domain, *reference)
            if identity in installed:
                return installed[identity]
            if identity in visiting or depth > 8 or identity not in retained:
                raise ValueError('Preparation backup has an incomplete or cyclic file graph')
            visiting.add(identity)
            entry = retained[identity]
            encoded, age = _read([str(root / entry['relative_filename']), *reference[1:]], with_age=True)
            if entry['graph']:
                encoded = _graph(encoded, lambda child: install(domain, child, depth + 1))
            destination = domains_by_name[domain](conn)
            # Album notes distinguish the raw original and individual outputs.
            if domain == 'album_notes':
                destination /= 'originals' if Path(reference[0]).parent.parent.name == 'originals' else 'outputs'
            digest = hashlib.sha256(encoded).hexdigest()
            result = [policy_documents._publish_encoded(destination, encoded, digest, age=age), digest, len(encoded)]
            installed[identity] = result
            visiting.remove(identity)
            return result

        relocated = {key: install(owner['domain'], owner['reference']) for key, owner in owners.items()}
        # This disposable copy changes coordinates only. Suppress UPDATE guards
        # and epoch invalidators together, then restore their exact definitions
        # within the same transaction. SQL rollback restores them on interruption.
        with policy_documents.coordinate_updates(conn, (owner['table'] for owner in owners.values())):
            for key, owner in owners.items():
                check_cancellable_work()
                query = 'UPDATE ' + owner['table'] + ' SET ' + ','.join(column + '=?' for column in owner['columns'])
                query += ' WHERE ' + ' AND '.join(column + '=?' for column in owner['keys'])
                conn.execute(query, (*relocated[key], *owner['keys'].values()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
