"""Read-only verification shared by clicked pools and paired projections."""
import hashlib
from pathlib import Path
from .. import fao

from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import document_plan, ranked_state as preparation


def source_row(conn, surface):
    return conn.execute("SELECT surface,COALESCE(substr(CAST(payload AS BLOB),1,?),X'') AS payload,built_at "
        'FROM main.rec_proposals WHERE surface=?', (preparation.MAX_SOURCE_BYTES+1, surface)).fetchone()


def retained(root, encoded):
    """Check exact content without publishing, fsyncing or reserving SQL writes."""
    if not 0 < len(encoded) <= preparation.files.MAX_BYTES:
        raise MigrationError('Ranked file exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest+'.json')
    with fao.open_reader(path) as stream:
        if stream.read(len(encoded)+1) != encoded:
            raise MigrationError('Ranked file differs from its original')
    return str(path), digest, len(encoded)


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Ranked verification requires an idle connection')
    surface, revision = header['surface'], header['preparation_revision']
    row = source_row(conn, surface)
    if row is None or preparation.read_original(conn, header) != bytes(row['payload']).decode('utf-8'):
        raise MigrationError('Ranked original differs from its retained source')
    fields, units = preparation.describe(conn, row, retain=retained)
    plan = document_plan.load(surface, tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    counts = dict.fromkeys(preparation.RELATIONS, 0)
    columns = {kind:conn.execute('PRAGMA table_info('+preparation.table(surface, suffix)+')').fetchall()
        for kind, suffix in preparation.RELATIONS.items()}
    count = 0
    for position, (kind, values) in enumerate(units):
        if position >= len(plan.units) or canonical(plan.units[position]) != canonical((kind, values)):
            raise MigrationError('Ranked durable plan differs from its original or references')
        captured = list(values)
        if kind == 'mode':
            captured[-1] = bytes.fromhex(captured[-1])
        keys = [(column[1], value) for column, value in zip(columns[kind][2:], captured) if column[5]]
        actual = conn.execute('SELECT * FROM '+preparation.table(surface, preparation.RELATIONS[kind])+
            ' WHERE surface=? AND preparation_revision=?'+''.join(' AND '+key+'=?' for key, _ in keys),
            (surface, revision, *(value for _, value in keys))).fetchone()
        if actual is None or tuple(actual)[2:] != tuple(captured):
            raise MigrationError('Ranked typed member differs from its original or references')
        counts[kind] += 1
        count += 1
    fields['unit_count'] = count
    if canonical(plan.fields) != canonical(fields) or len(plan.units) != count:
        raise MigrationError('Ranked plan fields or member count differ from their original')
    expected = dict.fromkeys(header.keys())
    expected.update(surface=surface, preparation_revision=revision, **fields,
        **{key:header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')})
    if any(header[key] != value for key, value in expected.items()):
        raise MigrationError('Ranked generation fields differ from their original or references')
    for kind, suffix in preparation.RELATIONS.items():
        actual = conn.execute('SELECT count(*) FROM '+preparation.table(surface, suffix)+
            ' WHERE surface=? AND preparation_revision=?', (surface, revision)).fetchone()[0]
        if actual != counts[kind]:
            raise MigrationError('Ranked typed members are missing or contain extras')
