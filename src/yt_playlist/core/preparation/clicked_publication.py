"""Publish complete clicked Home pools without blocking regular schema changes."""
import hashlib

from ..schema_contract import MigrationError
from . import ranked_state as preparation
from .mapped_publication import MappedPublication
from .ranked_verification import source_row, verify_document


def native(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='clicked_library_pools'").fetchone() is not None


OWNER = MappedPublication('clicked', preparation.JOBS['clicked'], 'prepared_clicked_pools_generations',
    'prepared_clicked_pools_heads', native, verify_document, preparation.read_original,
    source_column='surface', generation_column='preparation_revision')


def repair(conn, surface):
    if conn.in_transaction:
        raise MigrationError('Ranked file recovery requires an idle connection')
    header, row = preparation.head(conn, surface), source_row(conn, surface)
    if header is None or row is None or surface not in preparation.library_pool_state.SURFACES:
        raise MigrationError('Clicked file recovery requires its retained source')
    raw = bytes(row['payload'])
    if hashlib.sha256(raw).hexdigest() != header['raw_digest'] or row['built_at'] != header['built_at']:
        raise MigrationError('Clicked source changed; reconcile before file recovery')
    plan = preparation.prepare(conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt clicked plan differs from its captured manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    from .publication_cli import main as run
    return run(OWNER, __doc__, argv, label='Clicked pool', repair=repair,
               repair_flag='--repair-surface', repair_choices=preparation.library_pool_state.SURFACES)


if __name__ == '__main__':
    raise SystemExit(main())
