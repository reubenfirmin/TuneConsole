"""Verified recovery of the finite listening-evidence file domain."""
import hashlib
import json
from pathlib import Path

from . import evidence_ledger, policy_documents, fao
from .evidence_paths import record_id, record_path, maintenance_path
from .schema_inspection import definitions
from yt_playlist.repos import evidence_file_writes

MAX_BYTES = 8 * 1024 * 1024
PAGE_ROWS = 512


def directory(conn):
    return evidence_ledger.directory(conn)


def references(conn):
    root = directory(conn)
    owned = _native_owners(conn)
    for kind in sorted(evidence_ledger.EvidenceLedger.KINDS):
        yield from (path for path in sorted((root / kind).glob('*/*.json'))
                    if record_id(path) is None or record_id(path) in owned)


def _native_owners(conn):
    if not definitions(conn, ('collection_file_writes',)):
        return {}
    return {identity: (kind, key, digest, size) for (kind, key), (identity, digest, size)
            in evidence_file_writes.references(conn).items()}


def _retirements(root):
    from .evidence_retirement import Retirements
    return Retirements(root, path=maintenance_path(root, 1, 'retirements'))


def _read(path, *, with_age=False):
    with fao.read(path) as stream:
        data = stream.read(MAX_BYTES + 1)
        age = stream.info
    if len(data) > MAX_BYTES:
        raise ValueError('Listening evidence exceeds recovery memory budget')
    return (data, age) if with_age else data


def _owner(kind, token):
    if (kind not in evidence_ledger.EvidenceLedger.KINDS or not isinstance(token, str)
            or not (token.isdecimal() and str(int(token)) == token and int(token) > 0
                    or len(token) == 64 and not set(token) - set('0123456789abcdef'))):
        raise ValueError('Invalid listening evidence owner')


def _relative(kind, token):
    return record_path(Path(), kind, int(token)) if token.isdecimal() and len(token) != 64 else Path(kind, token[:2], token + '.json')


def _validate(kind, token, encoded, *, owners=None):
    _owner(kind, token)
    value = json.loads(encoded)
    if not isinstance(value, dict) and kind != 'artifact':
        raise ValueError('Invalid listening evidence record')
    identity = (hashlib.sha256(evidence_ledger.canonical(value)).hexdigest() if kind == 'artifact'
                else value.get('source_context') if kind == 'checkpoint' else value.get('id'))
    if record_id(_relative(kind, token)) is not None:
        expected = (owners or {}).get(int(token))
        if expected != (kind, str(identity), hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise ValueError('Listening evidence differs from its SQL owner')
    elif identity is not None and hashlib.sha256(str(identity).encode()).hexdigest() != token:
        raise ValueError('Listening evidence differs from its owner')
    return value


def export(conn, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    root = directory(conn)
    source = evidence_ledger.EvidenceLedger(root, 2**63 - 1)
    output = evidence_ledger.EvidenceLedger(target, 2**63 - 1)
    owners = _native_owners(conn)
    pages, entries, retirement = [], [], None

    def page():
        encoded = evidence_ledger.canonical(entries)
        name = 'page-' + str(len(pages)) + '.json'
        output._publish(target / name, encoded, 'manifest', name)
        pages.append([len(entries), hashlib.sha256(encoded).hexdigest()])
        entries.clear()

    # A snapshot may precede a just-published SQL descriptor. Exclude that
    # future menu and its responses rather than backing up a dangling owner.
    from . import mix_choices
    excluded = set()
    with policy_documents.retained_reader(root), source._budget():
        pending = _retirements(root)
        for identity, (kind, key, digest, size) in owners.items():
            path = record_path(root, kind, identity)
            if not path.is_file() and identity not in pending.files:
                raise FileNotFoundError('Missing SQL-owned listening evidence: ' + str(path))
        for kind in ('opportunity', 'response', 'forecast', 'artifact', 'report', 'checkpoint'):
            for path in sorted((root / kind).glob('*/*.json')):
                if record_id(path) is not None and record_id(path) not in owners:
                    continue
                encoded, age = _read(path, with_age=True)
                value = _validate(kind, path.stem, encoded, owners=owners)
                if (kind == 'opportunity' and value.get('precision') == mix_choices.PRECISION
                        and (not mix_choices.available(conn) or not conn.execute(
                            'SELECT 1 FROM mix_choice_menus WHERE opportunity_id=?', (value['id'],)).fetchone())):
                    if record_id(path) is not None:
                        raise ValueError('SQL-owned listening evidence is missing its menu')
                    excluded.add(value['id'])
                    continue
                if kind == 'response' and value.get('opportunity_id') in excluded:
                    continue
                relative = _relative(kind, path.stem)
                output._publish(target / relative, encoded, kind, path.stem, age=age)
                entries.append([kind, path.stem, len(encoded), hashlib.sha256(encoded).hexdigest()])
                if len(entries) == PAGE_ROWS:
                    page()
        if entries:
            page()
        # A snapshot can follow a partial unlink. Preserve the intent needed to
        # finish its orphan events and SQL reconciliation after restoration.
        if pending.offers or pending.checkpoints or pending.files:
            encoded, age = _read(pending.source_path, with_age=True)
            output._publish(target / '.retention-retirements', encoded, 'retirement', 'intent', age=age)
            retirement = [len(encoded), hashlib.sha256(encoded).hexdigest()]
    manifest = target / 'manifest.json'
    index = {'format': 'choice-evidence-1', 'pages': pages}
    if retirement is not None:
        index['retirements'] = retirement
    output._publish(manifest, evidence_ledger.canonical(index), 'manifest', 'index')
    return str(manifest)


def restore(conn, manifest):
    manifest = Path(manifest)
    index = json.loads(_read(manifest))
    if (set(index) not in ({'format', 'pages'}, {'format', 'pages', 'retirements'})
            or index['format'] != 'choice-evidence-1'):
        raise ValueError('Unsupported listening evidence backup')
    output = evidence_ledger.EvidenceLedger(directory(conn), 2**63 - 1)
    owners = _native_owners(conn)
    restored_ids = set()
    pending_files = set()
    with output._budget() as (_used, save):
        if 'retirements' in index:
            from .evidence_retirement import Retirements
            data = _read(manifest.parent / '.retention-retirements')
            if [len(data), hashlib.sha256(data).hexdigest()] != index['retirements']:
                raise ValueError('Listening evidence retirement intent failed verification')
            source = Retirements(manifest.parent)
            pending_files = source.files
            pending = _retirements(output.directory)
            pending.offers.update(source.offers)
            pending.checkpoints.update(source.checkpoints)
            pending.files.update(pending_files)
            pending.save()
        for number, (count, digest) in enumerate(index['pages']):
            encoded = _read(manifest.parent / ('page-' + str(number) + '.json'))
            entries = json.loads(encoded)
            if (hashlib.sha256(encoded).hexdigest() != digest or not 1 <= count <= PAGE_ROWS
                    or len(entries) != count):
                raise ValueError('Listening evidence manifest failed verification')
            for kind, token, size, digest in entries:
                # Validate tokens before constructing any path from the manifest.
                _owner(kind, token)
                relative = _relative(kind, token)
                source = manifest.parent / relative
                data, age = _read(source, with_age=True)
                if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('Listening evidence backup failed verification')
                _validate(kind, token, data, owners=owners)
                if record_id(relative) is not None:
                    restored_ids.add(int(token))
                output._publish(output.directory / relative, data, kind, token, age=age)
        if set(owners) - restored_ids - pending_files:
            raise ValueError('Listening evidence backup is missing a SQL owner')
        save(sum(fao.read(path).info.logical_bytes for path in references(conn)))
        output._advance_generation()
