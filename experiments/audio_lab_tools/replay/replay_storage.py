"""Own the exact external payloads referenced by a private consumer SQL copy."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from ..studies.external_checkpoint import RUN
from ..support.external_experiment import digest, save, stamp

WORK = RUN / 'library-replay'


def owned_rows(conn):
    from yt_playlist.core import policy_documents
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = []
    for owner in policy_documents.OWNERS:
        if owner not in tables:
            continue
        rows.extend({'table': owner, 'key': version, 'filename': filename,
                     'digest': checksum, 'offset': offset, 'bytes': size}
            for version, filename, checksum, offset, size in conn.execute(
                f'SELECT version,configuration_filename,content_digest,configuration_offset,byte_size '
                f'FROM {owner} ORDER BY version'))
    if 'prepared_outputs' in tables:
        rows.extend({'table': 'prepared_outputs', 'key': identity, 'surface': surface,
                     'chunk_key': key, 'filename': filename, 'digest': checksum, 'bytes': size}
            for identity, surface, key, filename, checksum, size in conn.execute(
                'SELECT id,surface,chunk_key,filename,content_digest,byte_size FROM prepared_outputs ORDER BY id'))
    return rows


def freeze(database, campaign):
    """Relocate filenames only after every referenced record has been verified.

    Read source payloads by their SQL-owned identity, never by sweeping the live
    native directory. Missing old payloads fail the new campaign explicitly.
    """
    from yt_playlist.core import policy_documents, recommendation_outputs
    database, campaign = Path(database).resolve(), Path(campaign).resolve()
    if not campaign.is_relative_to(WORK.resolve()) or database != campaign / 'source.db':
        raise ValueError('Payload relocation requires this campaign\'s private source.db')
    started = time.monotonic()
    root = campaign / 'payloads'
    source_sha256 = digest(database)
    files, replacements = {}, {}
    with closing(sqlite3.connect(database)) as conn:
        rows = owned_rows(conn)
        relocated = []
        for row in rows:
            source = Path(row['filename']).resolve()
            kind = 'recommendations' if row['table'] == 'prepared_outputs' else 'policies'
            token = (kind, str(source))
            if token not in replacements:
                with source.open('rb') as stream:
                    raw = stream.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError('Referenced payload exceeds copy bound: ' + str(source))
                checksum = hashlib.sha256(raw).hexdigest()
                if source.stem != checksum:
                    raise ValueError('Referenced payload filename/content differ: ' + str(source))
                target = root / kind / checksum[:2] / (checksum + '.json')
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if digest(target) != checksum:
                        raise ValueError('Existing private payload changed')
                else:
                    target.write_bytes(raw)
                    target.chmod(0o600)
                relative = str(target.relative_to(campaign))
                files[relative] = {'sha256': checksum, 'bytes': len(raw)}
                replacements[token] = str(target)
            target = replacements[token]
            if kind == 'policies':
                policy_documents.read(target, row['digest'], offset=row['offset'], size=row['bytes'])
            else:
                recommendation_outputs._read(target, row['digest'], row['bytes'],
                                             surface=row['surface'], chunk_key=row['chunk_key'])
            relocated.append({**row, 'filename': target})
        # No SQL pointer changes until all copies and every native record pass.
        with conn:
            for row in relocated:
                if row['table'] == 'prepared_outputs':
                    conn.execute('UPDATE prepared_outputs SET filename=? WHERE id=?', (row['filename'], row['key']))
                else:
                    conn.execute(f"UPDATE {row['table']} SET configuration_filename=? WHERE version=?",
                                 (row['filename'], row['key']))
            if owned_rows(conn) != relocated:
                raise ValueError('Payload relocation changed an owner or configuration')
    report = {'schema': 1, 'created_at': stamp(), 'source_database_sha256_before_relocation': source_sha256,
        'private_database_sha256_after_relocation': digest(database),
        'records': len(rows), 'files': files, 'bytes': sum(row['bytes'] for row in files.values()),
        'wall_s': time.monotonic() - started,
        'policy': 'Copy only SQL-referenced immutable policy/prepared-output payloads, verify full-file SHA256 and native record identity, then relocate filenames in this private database only.',
        'limitations': ['Native evidence-ledger history is not imported or replayed by this prepared-state consumer experiment.',
            'Older absolute references must still exist and match their content addresses; missing sources are not substituted.']}
    save(campaign / 'payload-manifest.json', report)
    verify(campaign, digest(campaign / 'payload-manifest.json'))
    return report


def verify(campaign, manifest_sha256):
    campaign = Path(campaign).resolve()
    if not campaign.is_relative_to(WORK.resolve()):
        raise ValueError('Expected an experiments-only payload archive')
    manifest = campaign / 'payload-manifest.json'
    if digest(manifest) != manifest_sha256:
        raise ValueError('Consumer payload manifest changed')
    report = json.loads(manifest.read_text())
    actual = {str(path.relative_to(campaign)) for path in (campaign / 'payloads').rglob('*') if path.is_file()}
    if actual != set(report['files']):
        raise ValueError('Consumer payload file set changed')
    for relative, expected in report['files'].items():
        path = (campaign / relative).resolve()
        if (not path.is_relative_to(campaign / 'payloads') or digest(path) != expected['sha256']
                or path.stat().st_size != expected['bytes']):
            raise ValueError('Consumer payload changed: ' + relative)
    return report


def guard_reads(patch, campaign):
    """Reject serving reads outside this campaign and its private working copy."""
    from yt_playlist.core import policy_documents, recommendation_outputs
    campaign = Path(campaign).resolve()
    if not campaign.is_relative_to(WORK.resolve()):
        raise ValueError('Expected an experiments-only consumer campaign')
    calls = {'policy_reads': 0, 'prepared_output_reads': 0, 'outside_boundary_reads': 0}
    for module, name, counter in ((policy_documents, 'read', 'policy_reads'),
                                  (recommendation_outputs, '_read', 'prepared_output_reads')):
        original = getattr(module, name)
        def guarded(filename, *args, _original=original, _counter=counter, **kwargs):
            if not Path(filename).resolve().is_relative_to(campaign):
                calls['outside_boundary_reads'] += 1
                raise ValueError('Consumer attempted an unarchived payload read: ' + str(filename))
            calls[_counter] += 1
            return _original(filename, *args, **kwargs)
        patch.setattr(module, name, guarded)
    return calls
