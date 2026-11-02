"""Publish coherent retained listening files without replacing proposal writers."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .. import listening_outputs as outputs, recommendation_outputs as files
from ..schema_contract import MigrationError
from . import listening_outputs as preparation
from .mapped_publication import MappedPublication


def source(conn, surface):
    row = conn.execute('SELECT '+','.join(preparation.DEFINITION.projections['rec_proposals'])+
        ' FROM main.rec_proposals WHERE surface=?', (surface,)).fetchone()
    return dict(row) if row is not None else None


def expected(conn, header, row):
    def retained(root, encoded):
        if (hashlib.sha256(encoded).hexdigest(), len(encoded)) != (header['digest'], header['byte_size']):
            raise MigrationError('Listening file differs from its installed original')
        if files._read(header['filename'],header['digest'],header['byte_size'],
                surface=header['file_surface'],chunk_key=header['chunk_key']) != encoded:
            raise MigrationError('Listening file bytes differ from its installed original')
        return header['filename'],header['digest'],header['byte_size']
    return preparation.describe(conn, row, retain=retained)


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Listening verification requires an idle connection')
    row = source(conn, header['surface'])
    if row is None or preparation.read_original(header) != bytes(row['payload']).decode():
        raise MigrationError('Listening original differs from its installed source')
    plan = expected(conn, header, row)
    fields = (plan.file_surface,plan.chunk_key,*plan.durable,plan.raw_digest,plan.raw_size,row['built_at'],*plan.metadata,len(plan.units))
    if tuple(header)[2:] != fields:
        raise MigrationError('Listening output metadata differs from its original')
    refs = conn.execute('SELECT * FROM '+preparation.REFS+' WHERE surface=? AND generation=? ORDER BY position',
        (header['surface'], header['generation'])).fetchall()
    if [tuple(ref)[2:] for ref in refs] != [(position,*unit) for position,unit in enumerate(plan.units)]:
        raise MigrationError('Listening output has incorrect, missing or extra chunk references')


class ListeningPublication(MappedPublication):
    def definitions(self):
        # TOTALS describes the current preparation heads and has no generation
        # columns. It remains mutable; runtime custody is the immutable map.
        for name, sql in super().definitions():
            if not name.startswith(preparation.TOTALS+'_mapped_'):
                yield name, sql

    def verify(self, conn, identity):
        captured = super().verify(conn, identity)
        totals = conn.execute('SELECT count(*),sum(g.file_surface=\'listening_chunk\'),coalesce(sum(g.byte_size),0) '
            'FROM '+self.table('map')+' m JOIN '+self.header+' g ON g.surface=m.source AND g.generation=m.generation WHERE m.stage=?',
            (identity,)).fetchone()
        if totals[0] > outputs.MAX_CHUNKS+2 or (totals[1] or 0) > outputs.MAX_CHUNKS or totals[2] > outputs.MAX_PUBLICATION_BYTES:
            raise MigrationError('Listening publication exceeds its owner or byte budget')
        manifest = self.capture(conn, identity, 'listening_analytics')
        if manifest is not None:
            for ref in conn.execute('SELECT * FROM '+preparation.REFS+' WHERE surface=? AND generation=? ORDER BY position',
                    (manifest['surface'], manifest['generation'])).fetchall():
                chunk = self.capture(conn, identity, 'listening_chunk:'+ref['chunk_key'])
                if chunk is None or chunk['raw_size'] != ref['encoded_bytes']:
                    raise MigrationError('Listening manifest has a missing or mismatched selected chunk')
        # Immutable mapped headers and exact raw index bytes jointly retain all
        # selected chunk edges. The independent staging link job stays mutable;
        # a newer chunk must not rewrite an earlier publication's relationships.
        try:
            conn.execute('BEGIN')
            if (self.proof(conn), self.stage(conn, identity)) != captured:
                raise MigrationError('Listening inputs changed during coherence verification; reconcile again')
        finally:
            conn.rollback()
        return captured


OWNER = ListeningPublication('listening-files', preparation.JOB, preparation.HEAD, preparation.CURRENT,
    outputs.available, verify_document, lambda conn,header:preparation.read_original(header),
    source_column='surface', generation_column='generation')


def read(conn, surface):
    if not outputs.owns(surface):
        return False, None
    owned = not conn.in_transaction
    try:
        if owned:
            conn.execute('BEGIN')
        identity = OWNER.selected(conn)
        if identity is None:
            return False, None
        header = OWNER.capture(conn, identity, surface)
    finally:
        if owned:
            conn.rollback()
    try:
        return True, preparation.read_original(header) if header is not None else None
    except (OSError, ValueError, MigrationError):
        # The native display owner treats unavailable/corrupt files as misses;
        # keep the selected owner rather than returning stale legacy bytes.
        return True, None


def repair(conn, surface):
    if conn.in_transaction:
        raise MigrationError('Listening file recovery requires an idle connection')
    header = conn.execute('SELECT g.* FROM '+preparation.CURRENT+' h JOIN '+preparation.HEAD+
        ' g USING(surface,generation) WHERE surface=?', (surface,)).fetchone()
    row = source(conn, surface)
    if (header is None or row is None or hashlib.sha256(bytes(row['payload'])).hexdigest() != header['raw_digest'] or
            row['built_at'] != header['source_at']):
        raise MigrationError('Listening source changed or is missing; reconcile before recovery')
    plan = preparation.prepare(conn, row)
    if plan.durable != (header['filename'],header['digest'],header['byte_size']):
        raise MigrationError('Recovered listening file differs from its captured manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-source')
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=128)
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                OWNER.publish(conn, stage=args.publish_stage)
                print(json.dumps({'selected':OWNER.selected(conn)}))
            elif args.repair_source is not None:
                repair(conn, args.repair_source)
                print(json.dumps({'repaired':args.repair_source}))
            else:
                for _ in range(args.batches):
                    current = OWNER.step(conn, batch_size=args.batch_size)
                    if current.complete:
                        break
                print(json.dumps(asdict(current)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Listening publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
