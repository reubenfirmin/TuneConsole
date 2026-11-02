"""Publish exact import reports after verifying typed fields and retained files."""
import hashlib

from .. import collection_recovery, spotify_job_state
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import collection_documents as preparation
from .document_publication import DocumentPublication, main as run, verify_paged_plan


class ImportPreparation:
    MAX_BYTES = preparation.MAX_SOURCE_BYTES

    @staticmethod
    def tables(source):
        return tuple(preparation.table('import', suffix) for suffix in ('generations', 'parts', 'heads'))

    @staticmethod
    def describe(source, text):
        return preparation.describe('import', text, None)

    @staticmethod
    def read_original(conn, header):
        return preparation.read_original(conn, 'import', header)


def verify_files(conn, header, described):
    if conn.in_transaction:
        raise MigrationError('Import file verification requires an idle connection')
    fields, value = described
    values, recovered = preparation.import_fields(conn, value)
    fields.update(values)
    if fields['state_present']:
        reference = tuple(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
        encoded = canonical({'format':collection_recovery.FORMAT, 'kind':'spotify-import',
                             'owner':header['source'], 'state':recovered})
        if reference[1:] != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise MigrationError('Import recovery differs from its original evidence')
        collection_recovery.read(*reference, kind='spotify-import', owner=header['source'])
        fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), reference))
    parts = conn.execute('SELECT position,filename,content_digest,byte_size FROM '+preparation.table('import', 'parts')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT ?',
        (header['source'], header['preparation_revision'], fields['part_count']+1)).fetchall()
    units = [('part', tuple(row)) for row in parts]
    if len(parts) != fields['part_count']:
        raise MigrationError('Import plan differs from its original parts')
    fields['unit_count'] = len(units)
    verify_paged_plan(header['source'], header, fields, units)
    return fields, parts


def verify_rows(conn, header, described):
    fields, parts = described
    expected = {'status':None, **dict.fromkeys(spotify_job_state.FIELDS), **fields}
    if any(header[key] != (float(value) if key in ('started_at', 'finished_at', 'created_at') and value is not None else value)
           for key, value in expected.items()):
        raise MigrationError('Prepared import fields differ from their original or account reference')
    current = conn.execute('SELECT position,filename,content_digest,byte_size FROM '+preparation.table('import', 'parts')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT ?',
        (header['source'], header['preparation_revision'], len(parts)+1)).fetchall()
    if list(map(tuple, current)) != list(map(tuple, parts)):
        raise MigrationError('Prepared import original custody changed during verification')


OWNER = DocumentPublication(spotify_job_state.KEY, ImportPreparation, preparation.JOBS['import'],
    spotify_job_state.available, verify_rows, generation_column='preparation_revision', verify_files=verify_files)


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    return run({'import':OWNER}, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
