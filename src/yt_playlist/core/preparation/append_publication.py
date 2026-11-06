"""Select verified TV/Radio operation custody while keeping installed writers."""
from functools import partial
import hashlib
from pathlib import Path
from .. import fao

from .. import append_work, collection_recovery, playlist_recipe_state, radio_storage
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import append_operations as preparation, document_plan
from .mapped_publication import MappedPublication


def source_row(name, conn, source):
    job = preparation.JOBS[name].definition
    owner, key, _, _ = preparation.SOURCES[name]
    row = conn.execute('SELECT '+','.join(job.projections[owner])+' FROM main.'+owner+' WHERE '+key+'=?', (source,)).fetchone()
    return dict(row) if row is not None else None


def retained(root, encoded):
    if not 0 < len(encoded) <= collection_recovery.MAX_BYTES:
        raise MigrationError('Append file exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest+'.json')
    with fao.open_reader(path) as stream:
        if stream.read(len(encoded)+1) != encoded:
            raise MigrationError('Append file differs from its original')
    return str(path), digest, len(encoded)


def retained_recovery(root, kind, owner, value):
    return retained(root, canonical({'format':collection_recovery.FORMAT, 'kind':kind, 'owner':owner, 'state':value}))


def verify_document(name, conn, header):
    if conn.in_transaction:
        raise MigrationError('Append verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    row = source_row(name, conn, source)
    if row is None or preparation.read_original(conn, name, header) != preparation.document(name, row)[0]:
        raise MigrationError('Append original differs from its retained source')
    fields, units = preparation.describe(name, conn, row, retain=retained, retain_recovery=retained_recovery)
    plan = document_plan.load(source, tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    counts, count = {'parts':0, 'items':0}, 0
    for position, (kind, values) in enumerate(units):
        if position >= len(plan.units) or canonical(plan.units[position]) != canonical((kind, values)):
            raise MigrationError('Append durable plan differs from its original or references')
        suffix = 'parts' if kind == 'part' else 'items'
        predicate, keys = ('position=?', values[:1]) if kind == 'part' else ('role=? AND list_position=? AND position=?', values[:3])
        actual = conn.execute('SELECT * FROM '+preparation.table(name, suffix)+
            ' WHERE source=? AND preparation_revision=? AND '+predicate, (source, revision, *keys)).fetchone()
        if actual is None or tuple(actual)[2:] != tuple(values):
            raise MigrationError('Append typed member differs from its original')
        counts[suffix] += 1
        count += 1
    fields['unit_count'] = count
    expected = dict.fromkeys(header.keys())
    expected.update(source=source, preparation_revision=revision, **fields,
        **{key:header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')})
    if (canonical(plan.fields) != canonical(fields) or len(plan.units) != count
            or any(header[key] != value for key, value in expected.items())):
        raise MigrationError('Append fields or member count differ from their original or references')
    for suffix, expected_count in counts.items():
        if conn.execute('SELECT count(*) FROM '+preparation.table(name, suffix)+
                ' WHERE source=? AND preparation_revision=?', (source, revision)).fetchone()[0] != expected_count:
            raise MigrationError('Append contains missing or extra typed members')


def original(name, conn, header):
    return preparation.read_original(conn, name, header)


NATIVE = {'tv_legacy':playlist_recipe_state.available, 'radio_legacy':radio_storage.available,
          'tv_native':append_work.available, 'radio_native':append_work.available}
OWNERS = {name:MappedPublication(name, preparation.JOBS[name], preparation.table(name, 'generations'),
    preparation.table(name, 'heads'), NATIVE[name], partial(verify_document, name), partial(original, name))
    for name in preparation.SOURCES}


def repair(name, conn, source):
    if conn.in_transaction:
        raise MigrationError('Append file recovery requires an idle connection')
    header = preparation.head(conn, name, source)
    row = source_row(name, conn, source)
    if header is None or row is None:
        raise MigrationError('Append recovery requires its retained source')
    text, _, kind = preparation.document(name, row)
    raw = text.encode() if text is not None else b''
    if (hashlib.sha256(raw).hexdigest() != header['raw_digest'] or kind != header['source_kind']
            or row.get('source_revision') != header['source_revision']):
        raise MigrationError('Append source changed; reconcile before file recovery')
    plan = preparation.prepare(name, conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt append differs from its retained manifest')


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    from .publication_cli import main as run
    return run(OWNERS, __doc__, argv, label='Append', repair=repair)


if __name__ == '__main__':
    raise SystemExit(main())
