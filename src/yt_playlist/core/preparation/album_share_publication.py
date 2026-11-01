"""Select exact frozen album choices while retaining installed album writers."""
import hashlib
from pathlib import Path
from .. import fao

from .. import album_tracklists, albums
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import album_shares as preparation, document_plan
from .mapped_publication import MappedPublication


def source_row(conn, source):
    row = conn.execute('SELECT '+','.join(preparation.DEFINITION.projections['album_share_snapshots'])+
        ' FROM main.album_share_snapshots WHERE browse_id=?', (source,)).fetchone()
    return dict(row) if row is not None else None


def retained(root, encoded):
    digest = hashlib.sha256(encoded).hexdigest()
    if not 0 < len(encoded) <= preparation.files.MAX_BYTES:
        raise MigrationError('Album original exceeds its file budget')
    path = Path(root) / digest[:2] / (digest+'.json')
    with fao.open_reader(path) as stream:
        if stream.read(len(encoded)+1) != encoded:
            raise MigrationError('Album original differs from its retained bytes')
    return str(path), digest, len(encoded)


def original(row):
    return {'browse_id':row['source'], **{key:bytes(row[key]).decode() for key in ('title','video_ids_json')}}


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Album share verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    row = source_row(conn, source)
    if row is None or preparation.read_original(conn, header) != original(row):
        raise MigrationError('Album share original differs from its installed source')
    fields, units = preparation.describe(conn, row, retain=retained)
    plan = document_plan.load(source, tuple(header[key] for key in ('plan_filename','plan_digest','plan_bytes')))
    counts, count = {'parts':0, 'items':0}, 0
    for position, (suffix, values) in enumerate(units):
        if position >= len(plan.units) or canonical(plan.units[position]) != canonical((suffix, values)):
            raise MigrationError('Album share plan differs from its original or references')
        actual = conn.execute('SELECT * FROM '+preparation.table(suffix)+
            ' WHERE source=? AND preparation_revision=? AND position=?', (source, revision, values[0])).fetchone()
        if actual is None or tuple(actual)[2:] != tuple(values):
            raise MigrationError('Album share member differs from its original or references')
        counts[suffix] += 1
        count += 1
    fields['unit_count'] = count
    expected = dict(source=source, preparation_revision=revision, **fields,
        **{key:header[key] for key in ('plan_filename','plan_digest','plan_bytes')})
    if canonical(plan.fields) != canonical(fields) or len(plan.units) != count or dict(header) != expected:
        raise MigrationError('Album share fields differ from their original or references')
    for suffix, expected_count in counts.items():
        if conn.execute('SELECT count(*) FROM '+preparation.table(suffix)+
                ' WHERE source=? AND preparation_revision=?', (source, revision)).fetchone()[0] != expected_count:
            raise MigrationError('Album share contains missing or extra members')


OWNER = MappedPublication('album-shares', preparation.JOB, preparation.table('generations'), preparation.table('heads'),
    album_tracklists.available, verify_document, preparation.read_original)


def selected_share(conn, stage, browse_id):
    header = OWNER.capture(conn, stage, browse_id)
    if header is None:
        header = conn.execute('SELECT g.* FROM '+OWNER.table('map')+' m JOIN '+OWNER.header+
            ' g ON g.source=m.source AND g.preparation_revision=m.generation JOIN main.album_provider_ids p '
            "ON p.provider='youtube' AND p.provider_id=g.source WHERE m.stage=? AND g.album_id=? "
            'ORDER BY p.is_primary DESC,p.provider_id LIMIT 1', (stage, albums.lookup(conn, browse_id))).fetchone()
    if header is None:
        return None
    return {'title':header['title'], 'video_ids':[row[0] for row in conn.execute(
        'SELECT raw_source_ref FROM '+preparation.table('items')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position', (header['source'], header['preparation_revision']))]}


def repair(conn, source):
    if conn.in_transaction:
        raise MigrationError('Album share file recovery requires an idle connection')
    header, row = preparation.head(conn, source), source_row(conn, source)
    if header is None or row is None or hashlib.sha256(canonical(original(row))).hexdigest() != header['raw_digest']:
        raise MigrationError('Album share source changed or is missing; reconcile before recovery')
    plan = preparation.prepare(conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename','plan_digest','plan_bytes')):
        raise MigrationError('Rebuilt album share differs from its captured manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    import json
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
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                print(json.dumps(asdict(stage)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Album share publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
