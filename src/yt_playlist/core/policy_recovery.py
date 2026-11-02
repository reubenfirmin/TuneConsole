"""Rehearse policy cleanup on a verified copy; never replace the source database.

Run with ``python -m yt_playlist.core.policy_recovery --help``. The output keeps
an untouched verified backup as well as the compact recovery candidate.
"""
import argparse
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from yt_playlist.core import (album_description_files, audio_annotation_files,
                              db_space, group_documents, legacy_evidence, metadata_delivery_files,
                              metadata_destination_files, policy_config,
                              playback_feedback_backup, policy_documents, recommendation_outputs,
                              saved_preparation_files, preparation_files, sample_files)
from yt_playlist.core.migrations import migrate
from yt_playlist.repos import policy_storage


def _progress(message):
    print(message, flush=True)


def _open(path, *, readonly=False):
    from .db_handles import connect
    conn = connect(path.as_uri() + '?mode=ro', uri=True) if readonly else connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _verify(conn):
    result = [row[0] for row in conn.execute('PRAGMA integrity_check')]
    if result != ['ok']:
        raise ValueError(f'Database integrity check failed: {result[:5]}')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Database foreign-key check failed')


def _counts(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%'")]
    return {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in tables if name not in {'schema_migrations', 'rec_serving_policies',
                                              'rec_policy_bases', 'rec_policy_variants',
                                              'recommendation_policies', 'recommendation_policy_bodies'}}


def _missing_references(conn):
    if policy_storage.typed(conn):
        counts = dict(conn.execute('SELECT kind,count(*) FROM recommendation_impression_policies '
                                   'WHERE policy_version IS NULL GROUP BY kind'))
        return {'direct': counts.get('primary', 0), 'pool': counts.get('pool', 0)}
    direct = conn.execute('SELECT count(*) FROM rec_served_impressions i WHERE NOT EXISTS ('
        'SELECT 1 FROM rec_serving_policies p WHERE p.model_version=i.model_version)').fetchone()[0]
    pool = conn.execute("SELECT count(*) FROM rec_served_impressions i WHERE "
        "json_extract(i.provenance,'$.pool_policy') IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM rec_serving_policies p WHERE p.model_version=CAST("
        "json_extract(i.provenance,'$.pool_policy') AS TEXT))").fetchone()[0]
    return {'direct': direct, 'pool': pool}


def _fingerprint(configuration):
    return hashlib.sha256(policy_config.canonical(configuration).encode()).hexdigest()


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _file_exports(conn, target, *, backup_root=None):
    """Inventory explicit exporters once, using the captured SQL layout."""
    tables, target = _tables(conn), Path(target)
    def root(domain):
        return Path(backup_root) / domain if backup_root is not None else target.with_name(target.name + '.' + domain)
    from . import file_owners
    exports = [(None, file_owners.export, root)]
    from . import metadata_runtime, metadata_runtime_files, served_facts, choice_evidence_files
    exports.append(('choice_evidence', choice_evidence_files.export, root('choice_evidence')))
    if served_facts.available(conn):
        exports.append(('served', served_facts.export, root('served')))
    if metadata_runtime.active(conn):
        exports.append(('runtime_metadata', metadata_runtime_files.export, root('runtime_metadata')))
    if 'sample_attributes' in tables:
        exports.append(('samples', sample_files.export, root('samples')))
    if 'action_backups' in tables:
        from . import action_backup_files
        exports.append(('actions', action_backup_files.export, root('actions')))
    if 'album_feedback' in tables:
        from . import collection_recovery
        exports.append(('collections', collection_recovery.export, root('collections')))
    if 'audio_annotations' in tables:
        exports.append(('annotations', audio_annotation_files.export, root('annotations')))
    if 'recommendation_policies' in tables:
        exports.append(('policies', policy_documents.export, root('policies')))
    if 'album_description_requests' in tables:
        exports.append(('descriptions', album_description_files.export, root('descriptions')))
    if 'group_versions' in tables:
        exports.append(('groups', group_documents.export, root('groups')))
    if 'prepared_outputs' in tables:
        exports.append(('recommendations', recommendation_outputs.export, root('recommendations')))
    if 'metadata_deliveries' in tables:
        exports.append(('metadata', metadata_delivery_files.export, root('metadata')))
    if 'published_metadata_destination_deliveries' in tables:
        exports.append(('metadata_preparation', metadata_destination_files.export, root('metadata_preparation')))
    if saved_preparation_files.available(conn):
        exports.append(('saved_preparation', saved_preparation_files.export, root('saved_preparation')))
    if preparation_files.available(conn):
        exports.append(('preparation', preparation_files.export, root('preparation')))
    if 'legacy_catalog_evidence' in tables:
        exports.append(('archives', legacy_evidence.export, root('archives')))
    return exports


def _export_files(conn, target, *, backup_root=None):
    """Copy the explicit file owners from the same snapshot as SQLite."""
    manifests = {}
    for name, export, root in _file_exports(conn, target, backup_root=backup_root):
        manifest = export(conn, root)
        if name is None:
            manifests.update(manifest)
        else:
            manifests[name] = manifest
    return manifests


def _restore_files(conn, manifests):
    """Restore domain files before allowing a disposable database to be used."""
    from . import file_owners
    if set(manifests) - set(file_owners.owners()) - {'annotations', 'policies', 'descriptions', 'groups', 'recommendations',
                         'metadata', 'metadata_preparation', 'saved_preparation', 'preparation', 'archives',
                         'playback_feedback', 'collections', 'actions', 'samples', 'runtime_metadata', 'served', 'choice_evidence'}:
        raise ValueError('Unsupported recovery file domain')
    file_owners.restore(conn, manifests)
    if 'samples' in manifests:
        sample_files.restore(conn, manifests['samples'])
    if 'collections' in manifests:
        from . import collection_recovery
        collection_recovery.restore(conn,manifests['collections'])
    if 'actions' in manifests:
        from . import action_backup_files
        action_backup_files.restore(conn,manifests['actions'])
    if 'annotations' in manifests:
        audio_annotation_files.restore(conn, manifests['annotations'])
    if 'policies' in manifests:
        policy_documents.restore(conn, manifests['policies'])
    if 'descriptions' in manifests:
        album_description_files.restore(conn, manifests['descriptions'])
    if 'groups' in manifests:
        group_documents.restore(conn, manifests['groups'])
    if 'recommendations' in manifests:
        recommendation_outputs.restore(conn, manifests['recommendations'])
    if 'metadata' in manifests:
        metadata_delivery_files.restore(conn, manifests['metadata'])
    if 'runtime_metadata' in manifests:
        from . import metadata_runtime_files
        metadata_runtime_files.restore(conn, manifests['runtime_metadata'])
    if 'served' in manifests:
        from . import served_facts
        served_facts.restore(conn, manifests['served'])
    if 'choice_evidence' in manifests:
        from . import choice_evidence_files
        choice_evidence_files.restore(conn, manifests['choice_evidence'])
    if 'metadata_preparation' in manifests:
        metadata_destination_files.restore(conn, manifests['metadata_preparation'])
    if 'saved_preparation' in manifests:
        saved_preparation_files.restore(conn, manifests['saved_preparation'])
    if 'preparation' in manifests:
        preparation_files.restore(conn, manifests['preparation'])
    if 'archives' in manifests:
        legacy_evidence.restore(conn, manifests['archives'])
    if 'playback_feedback' in manifests:
        playback_feedback_backup.restore(conn, manifests['playback_feedback'])


def _clean_copy(conn, now, progress):
    if policy_storage.typed(conn):
        deleted = 0
        while True:
            candidates, cursor = policy_storage.retention_candidates(conn, now)
            with conn:
                result = policy_storage.prune(conn, candidates, cursor)
            deleted += result['policies_deleted']
            if not cursor:
                break
            progress(f'Collected {deleted} unused policies')
        fingerprints, after = {}, ''
        while batch := conn.execute('SELECT version FROM recommendation_policies WHERE version>? '
                                     'ORDER BY version LIMIT 256', (after,)).fetchall():
            for row in batch:
                fingerprints[row[0]] = _fingerprint(policy_storage.configuration(conn, row[0]))
            after = batch[-1][0]
        return deleted, fingerprints
    protected = policy_storage.protected_references(conn)
    candidates = [row[0] for row in conn.execute('SELECT p.model_version FROM rec_serving_policies p '
        'WHERE created_at<? AND ' + policy_storage.UNSERVED, (now - policy_storage.GRACE_SECONDS,))
        if row[0] not in protected]
    for start in range(0, len(candidates), 256):
        batch = candidates[start:start + 256]
        with conn:
            marks = ','.join('?' for _ in batch)
            conn.execute(f'DELETE FROM rec_serving_policies WHERE model_version IN ({marks})', batch)
        if start % 8192 == 0:
            progress(f'Collected {min(start + 256, len(candidates))}/{len(candidates)} unused policies')
    fingerprints, after = {}, ''
    while True:
        batch = conn.execute('SELECT * FROM rec_serving_policies WHERE model_version>? '
                             'ORDER BY model_version LIMIT 256', (after,)).fetchall()
        if not batch:
            break
        rows, bases = [], {}
        for row in batch:
            original = policy_storage.configuration(conn, row['model_version'])
            fingerprints[row['model_version']] = _fingerprint(original)
            packed, base = policy_config.pack(original, row['created_at'], version=row['model_version'])
            rows.append(packed)
            bases[base[0]] = base
        with conn:
            policy_storage.publish(conn, rows, bases.values())
            conn.executemany('UPDATE rec_serving_policies SET configuration=? WHERE model_version=?',
                             [(row[1], row[0]) for row in rows])
        after = batch[-1]['model_version']
        if len(fingerprints) % 4096 == 0:
            progress(f'Shared configuration bodies for {len(fingerprints)} retained policies')
    with conn:
        conn.execute('DELETE FROM rec_policy_bases WHERE NOT EXISTS ('
            'SELECT 1 FROM rec_policy_variants v WHERE v.base_version=rec_policy_bases.base_version)')
    return len(candidates), fingerprints


def recover(source, output_dir, *, now=None, progress=_progress):
    source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
    if not source.is_file():
        raise ValueError(f'Source database does not exist: {source}')
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup, work, compact = (output_dir / name for name in ('verified-original.db', 'working.db', 'compact.db'))
    with closing(_open(source, readonly=True)) as reader:
        size = reader.execute('PRAGMA page_count').fetchone()[0] * reader.execute('PRAGMA page_size').fetchone()[0]
        db_space.require_space(output_dir, 3 * size)
        progress(f'Backing up {size} database bytes to {backup}')
        deadline = time.monotonic() + 300
        def copied(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('Online backup exceeded five minutes; stop application writers and retry in a new directory')
        with ExitStack() as retained:
            from . import file_owners
            retained.enter_context(file_owners.readers(source, str(id(reader))))
            from . import choice_evidence_files
            retained.enter_context(policy_documents.retained_reader(choice_evidence_files.directory(reader)))
            from . import served_facts
            if served_facts.available(reader):
                retained.enter_context(policy_documents.retained_reader(served_facts.directory(reader)))
            if 'sample_attributes' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(sample_files.directory(reader)))
            if 'album_feedback' in _tables(reader):
                from . import collection_recovery
                retained.enter_context(policy_documents.retained_reader(collection_recovery.directory(reader)))
            if 'recommendation_policies' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(policy_documents.directory(reader)))
            if 'group_versions' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(group_documents.directory(reader)))
            if 'prepared_outputs' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(recommendation_outputs.directory(reader)))
            if 'metadata_deliveries' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(metadata_delivery_files.directory(reader)))
                from . import metadata_runtime_files
                retained.enter_context(policy_documents.retained_reader(metadata_runtime_files.directory(reader)))
            if 'published_metadata_destination_deliveries' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(metadata_destination_files.directory(reader)))
            if saved_preparation_files.available(reader):
                retained.enter_context(policy_documents.retained_reader(saved_preparation_files.directory(reader)))
            for directory in preparation_files.directories(reader):
                retained.enter_context(policy_documents.retained_reader(directory))
            if 'legacy_catalog_evidence' in _tables(reader):
                retained.enter_context(policy_documents.retained_reader(legacy_evidence.directory(reader)))
            # Export files and SQLite under one pinned view. The verified DB
            # itself stays byte-for-byte logical evidence; its file manifest
            # permits restoring it without depending on the source directory.
            with playback_feedback_backup.pinned(reader, backup,
                    backup_root=output_dir / 'original-files') as feedback:
                backup_files = _export_files(reader, backup, backup_root=output_dir / 'original-files')
                if feedback is not None:
                    backup_files['playback_feedback'] = feedback
                with closing(_open(backup)) as target:
                    reader.backup(target, pages=512, progress=copied)
                    progress('Checking integrity and foreign keys of the untouched backup')
                    _verify(target)
    # The verified backup is closed and never opened for writes again.
    shutil.copyfile(backup, work)
    backup.chmod(0o600)
    work.chmod(0o600)
    with closing(_open(work)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        # Erasure is unnecessary on this disposable private copy: VACUUM INTO
        # excludes freed pages. Avoid journaling/zeroing gigabytes of unused JSON.
        conn.execute('PRAGMA secure_delete=OFF')
        _restore_files(conn, backup_files)
        # Older recovery candidates need the supported compatibility floor.
        # Conversions above it are independent preparation, including unknown
        # saved proposal/session data which policy cleanup must not retire.
        from yt_playlist.core.migrations.registry import MIGRATIONS
        from yt_playlist.core.migrations.runner import history, validate_version
        if history(conn, MIGRATIONS) < 79:
            migrate(conn, registry=MIGRATIONS[:79])
            validate_version(conn, 79)
        migrate(conn)
        # Establish the cleanup baseline after the supported floor and bounded
        # regular changes, before policy cleanup can modify any retained data.
        _verify(conn)
        counts, missing = _counts(conn), _missing_references(conn)
        policy_table = 'recommendation_policies' if policy_storage.typed(conn) else 'rec_serving_policies'
        before = conn.execute(f'SELECT count(*) FROM {policy_table}').fetchone()[0]
        deleted, expected = _clean_copy(conn, time.time() if now is None else now, progress)
        with playback_feedback_backup.pinned(conn, compact) as feedback:
            compact_files = _export_files(conn, compact)
            if feedback is not None:
                compact_files['playback_feedback'] = feedback
        preparation_directories = preparation_files.directories(conn)
        progress(f'Creating compact database at {compact}')
        conn.execute('VACUUM INTO ?', (str(compact),))
    compact.chmod(0o600)
    with closing(_open(compact)) as conn:
        _restore_files(conn, compact_files)
    with closing(_open(compact, readonly=True)) as conn:
        _verify(conn)
        if _counts(conn) != counts:
            raise ValueError('Recovery changed non-policy table counts; source and verified backup are untouched')
        if _missing_references(conn) != missing:
            raise ValueError('Recovery changed impression reference resolution')
        modern = policy_storage.typed(conn)
        policy_query = 'SELECT version FROM recommendation_policies' if modern else 'SELECT model_version FROM rec_serving_policies'
        actual = {row[0]: _fingerprint(policy_storage.configuration(conn, row[0]))
                  for row in conn.execute(policy_query)}
        if actual != expected:
            raise ValueError('Recovery changed retained policy configurations')
        report = {'source': str(source), 'verified_backup': str(backup), 'compact_database': str(compact),
            'original_bytes': backup.stat().st_size, 'compact_bytes': compact.stat().st_size,
            'policies_before': before, 'policies_deleted': deleted, 'policies_retained': len(actual),
            'shared_bodies': conn.execute('SELECT count(*) FROM ' +
                ('recommendation_policy_bodies' if modern else 'rec_policy_bases')).fetchone()[0],
            'verified_backup_file_manifests': backup_files,
            'non_policy_table_counts': counts, 'existing_missing_impression_references': missing,
            'integrity_check': 'ok', 'foreign_key_check': 'ok', 'retained_policy_fingerprints': 'identical',
            'source_replaced': False}
    (output_dir / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    # Only the disposable working copy is removed; leave the verified original.
    work.unlink()
    for domain in compact_files:
        if domain == 'preparation':
            continue
        working_files = work.with_name(work.name + '.' + domain)
        if working_files.exists():
            shutil.rmtree(working_files)
    working_preparation = work.with_name(work.name + '.preparation')
    if 'preparation' in compact_files:
        for directory in preparation_directories:
            if directory.exists():
                shutil.rmtree(directory)
    for domain, folder in (('metadata_preparation', 'metadata-deliveries'),
                           ('saved_preparation', 'saved-outputs')):
        if domain in compact_files:
            shutil.rmtree(working_preparation / folder)
    if working_preparation.exists() and not any(working_preparation.iterdir()):
        working_preparation.rmdir()
    working_inbox = playback_feedback_backup._queue(work)
    working_inbox.path.unlink(missing_ok=True)
    working_inbox.path.with_name(working_inbox.path.name + '.lock').unlink(missing_ok=True)
    if working_inbox.directory.exists():
        shutil.rmtree(working_inbox.directory)
    progress(json.dumps({key: value for key, value in report.items() if key != 'non_policy_table_counts'}, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='Source SQLite database, opened read-only')
    parser.add_argument('--output-dir', required=True, type=Path, help='New directory on a filesystem with ample free space')
    args = parser.parse_args()
    recover(args.source, args.output_dir)


if __name__ == '__main__':
    main()
