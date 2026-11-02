"""Select retained discovery explanations without replacing writable candidates."""
from dataclasses import asdict
from functools import partial
import hashlib
import json

from .. import collection_recovery, genre_classifications, recommendation_outputs as files
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import discovery_artists as preparation, document_plan
from .mapped_publication import MappedPublication, Stage


MAX_KEY_BYTES = 4096


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='discovery_origins'").fetchone() is not None


def source(conn, name, key):
    job = preparation.JOBS[name].definition
    return job._read(conn, 'discovered_artists', json.dumps([key], ensure_ascii=False, separators=(',', ':')))


def verify_document(name, conn, header):
    if conn.in_transaction:
        raise MigrationError('Discovery explanation verification requires an idle connection')
    row = source(conn, name, header['source'])
    if row is None:
        raise MigrationError('Discovery explanation has no installed source')
    raw, fields, explanation = preparation.describe(name, conn, row)
    if preparation.read_original(conn, name, header) != json.loads(raw):
        raise MigrationError('Discovery explanation original differs from its installed source')
    identity = fields['artist_id'] if fields['artist_id'] is not None else header['source']
    encoded = canonical({'format': collection_recovery.FORMAT, 'kind': 'discovery-candidate',
                         'owner': str(identity), 'state': explanation})
    expected = {'source': header['source'], 'preparation_revision': header['preparation_revision'],
                **fields, 'unit_count': fields['part_count'],
                'recovery_filename': header['recovery_filename'],
                'recovery_digest': hashlib.sha256(encoded).hexdigest(), 'recovery_bytes': len(encoded),
                **{key: header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')}}
    if dict(header) != expected:
        raise MigrationError('Discovery explanation metadata differs from its installed source')
    if collection_recovery.read(header['recovery_filename'], header['recovery_digest'], header['recovery_bytes'],
            kind='discovery-candidate', owner=identity) != explanation:
        raise MigrationError('Discovery explanation file differs from its installed source')
    plan = document_plan.load(header['source'], tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    expected_fields = {key: value for key, value in expected.items()
                       if key not in ('source', 'preparation_revision', 'plan_filename', 'plan_digest', 'plan_bytes')}
    if plan.fields != expected_fields or len(plan.units) != fields['part_count']:
        raise MigrationError('Discovery explanation plan differs from its retained fields')
    parts = conn.execute('SELECT * FROM '+preparation.table(name, 'parts')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position',
        (header['source'], header['preparation_revision'])).fetchall()
    if len(parts) != fields['part_count']:
        raise MigrationError('Discovery explanation has missing or extra original parts')
    text = raw.decode()
    for position, part in enumerate(parts):
        expected_bytes = files.encode(header['source'], 'original:'+str(position),
            text[position*preparation.PART_CHARACTERS:(position+1)*preparation.PART_CHARACTERS])
        reference = (part['filename'], part['content_digest'], part['byte_size'])
        if (part['position'] != position or tuple(plan.units[position]) != (position, *reference)
                or part['content_digest'] != hashlib.sha256(expected_bytes).hexdigest()
                or part['byte_size'] != len(expected_bytes)
                or files._read(*reference, surface=header['source'], chunk_key='original:'+str(position)) != expected_bytes):
            raise MigrationError('Discovery explanation original part differs from its installed source')


class ArtistPublication(MappedPublication):
    def step(self, conn, *, batch_size=8):
        """Plan bounded keys before reserving the writer, then fence the proof."""
        if type(batch_size) is not int or not 1 <= batch_size <= 8:
            raise ValueError('Discovery explanation map batches require 1 to 8 links')
        self.install(conn)
        try:
            conn.execute('BEGIN')
            proof = self.proof(conn)
            previous = conn.execute('SELECT * FROM '+self.table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
            previous = Stage(*previous) if previous else None
            stage = previous if previous and (previous.input_revision, previous.custody_revision) == (
                proof[0].input_revision, proof[1]) else Stage(0, proof[0].input_revision, proof[1], None, 0, 0)
            if stage.complete:
                return stage
            rows = conn.execute('SELECT substr(CAST(source AS BLOB),1,?),preparation_revision FROM '+self.current+
                (' WHERE source>?' if stage.after_source is not None else '')+' ORDER BY source LIMIT ?',
                (MAX_KEY_BYTES+1, *((stage.after_source,) if stage.after_source is not None else ()), batch_size+1)).fetchall()
            links = []
            for key, generation in rows[:batch_size]:
                if len(key) > MAX_KEY_BYTES:
                    raise MigrationError('Discovery explanation key exceeds its publication byte budget')
                links.append((bytes(key).decode(), generation))
            conn.rollback()
            conn.execute('BEGIN IMMEDIATE')
            latest = conn.execute('SELECT * FROM '+self.table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
            if self.proof(conn) != proof or (Stage(*latest) if latest else None) != previous:
                raise MigrationError('Discovery explanation inputs changed before map publication; retry')
            if not stage.id:
                identity = conn.execute('INSERT INTO '+self.table('stages')+
                    '(input_revision,custody_revision,copied,complete) VALUES (?,?,0,0)',
                    (stage.input_revision, stage.custody_revision)).lastrowid
                stage = self.stage(conn, identity)
            for key, generation in links:
                conn.execute('INSERT INTO '+self.table('custody')+' VALUES (?,?) ON CONFLICT DO NOTHING', (key, generation))
                conn.execute('INSERT INTO '+self.table('map')+' VALUES (?,?,?)', (stage.id, key, generation))
            conn.execute('UPDATE '+self.table('stages')+' SET after_source=?,copied=?,complete=? WHERE id=?',
                (links[-1][0] if links else stage.after_source, stage.copied+len(links), int(len(rows)<=batch_size), stage.id))
            stage = self.stage(conn, stage.id)
            conn.commit()
            return stage
        finally:
            conn.rollback()


def read_original(name, conn, header):
    return preparation.read_original(conn, name, header)


OWNERS = {name: ArtistPublication(name, worker, preparation.table(name, 'generations'),
    preparation.table(name, 'heads'), native, partial(verify_document, name), partial(read_original, name))
    for name, worker in preparation.JOBS.items()}


def owner(conn):
    return OWNERS['artists_classified' if genre_classifications.available(conn) else 'artists_legacy']


def relation(conn):
    """Read file-backed fields while preserving installed identities and offer precision.

    Canonical subject merging and verified visit counters are a separate owner.
    Keeping this projection explicit also leaves every legacy writer available.
    """
    if native(conn):
        return 'main.discovered_artists'
    publication = owner(conn)
    revision = publication.selected(conn)
    if revision is None:
        return 'main.discovered_artists'
    fields = ('score', 'thumbnail', 'found_at', 'first_shown', 'last_shown', 'offered_count',
              'legacy_offered_count', 'legacy_first_shown', 'legacy_last_shown', 'genre_key',
              'recovery_filename', 'recovery_digest', 'recovery_bytes', 'explanation_status')
    genre = 'g.captured_genre genre,' if publication.name == 'artists_legacy' else 'g.captured_genre unresolved_genre,'
    return ('(SELECT g.source artist,g.captured_artist_id artist_id,g.artist_id preparation_artist_id,'+
        genre+'NULL because,NULL fits,'+
        ','.join('g.'+field for field in fields)+' FROM '+publication.table('map')+
        ' m JOIN '+publication.header+' g ON g.source=m.source AND g.preparation_revision=m.generation '
        'WHERE m.stage='+str(revision)+')')


def repair(conn, key):
    if conn.in_transaction:
        raise MigrationError('Discovery explanation recovery requires an idle connection')
    publication = owner(conn)
    header = preparation.head(conn, publication.name, key)
    row = source(conn, publication.name, key)
    if header is None or row is None or preparation.describe(publication.name, conn, row)[1]['raw_digest'] != header['raw_digest']:
        raise MigrationError('Discovery explanation source changed or is missing; reconcile before recovery')
    plan = preparation.prepare(publication.name, conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[field] for field in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Recovered discovery explanation differs from its retained manifest')


def validate_installed(conn):
    for publication in OWNERS.values():
        publication.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=tuple(OWNERS))
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-source')
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=8)
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    publication = OWNERS[args.job]
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                publication.publish(conn, stage=args.publish_stage)
                result = {'selected': publication.selected(conn)}
            elif args.repair_source is not None:
                if publication != owner(conn):
                    raise MigrationError('Discovery explanation recovery requires the installed artist format')
                repair(conn, args.repair_source)
                result = {'repaired': args.repair_source}
            else:
                for _ in range(args.batches):
                    stage = publication.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Discovery explanation publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
