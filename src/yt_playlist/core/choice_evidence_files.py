"""Verified recovery of the finite listening-evidence file domain."""
import hashlib
import json
from pathlib import Path

from . import evidence_ledger, policy_documents, fao

MAX_BYTES = 8 * 1024 * 1024
PAGE_ROWS = 512


def directory(conn):
    return evidence_ledger.directory(conn)


def references(conn):
    root = directory(conn)
    for kind in sorted(evidence_ledger.EvidenceLedger.KINDS):
        yield from sorted((root / kind).glob('*/*.json'))


def _read(path, *, with_age=False):
    with fao.open_reader(path) as stream:
        data = stream.read(MAX_BYTES + 1)
        age = stream.info
    if len(data) > MAX_BYTES:
        raise ValueError('Listening evidence exceeds recovery memory budget')
    return (data, age) if with_age else data


def _owner(kind, token):
    if (kind not in evidence_ledger.EvidenceLedger.KINDS or len(token) != 64
            or any(c not in '0123456789abcdef' for c in token)):
        raise ValueError('Invalid listening evidence owner')


def _validate(kind, token, encoded):
    _owner(kind, token)
    value = json.loads(encoded)
    if not isinstance(value, dict) and kind != 'artifact':
        raise ValueError('Invalid listening evidence record')
    identity = (hashlib.sha256(evidence_ledger.canonical(value)).hexdigest() if kind == 'artifact'
                else value.get('source_context') if kind == 'checkpoint' else value.get('id'))
    if identity is not None and hashlib.sha256(str(identity).encode()).hexdigest() != token:
        raise ValueError('Listening evidence differs from its owner')
    return value


def export(conn, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    root = directory(conn)
    source = evidence_ledger.EvidenceLedger(root, 2**63 - 1)
    output = evidence_ledger.EvidenceLedger(target, 2**63 - 1)
    pages, entries = [], []

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
        for kind in ('opportunity', 'response', 'forecast', 'artifact', 'report', 'checkpoint'):
            for path in sorted((root / kind).glob('*/*.json')):
                encoded, age = _read(path, with_age=True)
                value = _validate(kind, path.stem, encoded)
                if (kind == 'opportunity' and value.get('precision') == mix_choices.PRECISION
                        and (not mix_choices.available(conn) or not conn.execute(
                            'SELECT 1 FROM mix_choice_menus WHERE opportunity_id=?', (value['id'],)).fetchone())):
                    excluded.add(value['id'])
                    continue
                if kind == 'response' and value.get('opportunity_id') in excluded:
                    continue
                relative = Path(kind, path.stem[:2], path.name)
                output._publish(target / relative, encoded, kind, path.stem, age=age)
                entries.append([kind, path.stem, len(encoded), hashlib.sha256(encoded).hexdigest()])
                if len(entries) == PAGE_ROWS:
                    page()
        if entries:
            page()
    manifest = target / 'manifest.json'
    output._publish(manifest, evidence_ledger.canonical({'format': 'choice-evidence-1', 'pages': pages}), 'manifest', 'index')
    return str(manifest)


def restore(conn, manifest):
    manifest = Path(manifest)
    index = json.loads(_read(manifest))
    if set(index) != {'format', 'pages'} or index['format'] != 'choice-evidence-1':
        raise ValueError('Unsupported listening evidence backup')
    output = evidence_ledger.EvidenceLedger(directory(conn), 2**63 - 1)
    with output._budget() as (_used, save):
        for number, (count, digest) in enumerate(index['pages']):
            encoded = _read(manifest.parent / ('page-' + str(number) + '.json'))
            entries = json.loads(encoded)
            if (hashlib.sha256(encoded).hexdigest() != digest or not 1 <= count <= PAGE_ROWS
                    or len(entries) != count):
                raise ValueError('Listening evidence manifest failed verification')
            for kind, token, size, digest in entries:
                # Validate tokens before constructing any path from the manifest.
                _owner(kind, token)
                source = manifest.parent / kind / token[:2] / (token + '.json')
                data, age = _read(source, with_age=True)
                if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('Listening evidence backup failed verification')
                _validate(kind, token, data)
                output._publish(output.directory / kind / token[:2] / (token + '.json'), data, kind, token, age=age)
        save(sum(fao.stat(path).logical_bytes for path in references(conn)))
        output._advance_generation()
