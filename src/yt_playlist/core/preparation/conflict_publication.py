"""Select verified conflict evidence without retiring compatible enrichment writers."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

from .. import metadata_evidence, metadata_legacy
from ..migrations.runner import MigrationError
from ..policy_documents import _publish_encoded
from . import metadata_conflicts as preparation
from .mapped_publication import MappedPublication


VIEW = 'prepared_metadata_conflict_review'


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Conflict verification requires an idle connection')
    row = preparation._source(conn, header['conflict_id'])
    if row is None:
        raise MigrationError('Retained conflict source is missing')
    source, encoded, kind, candidates = preparation._document(row)
    if (preparation.read_original(header) != source or header['digest'] != hashlib.sha256(encoded).hexdigest()
            or header['byte_size'] != len(encoded)):
        raise MigrationError('Prepared conflict original differs from its source')
    try:
        conn.execute('BEGIN')
        entry, recording, video, field, scope, _, disposition = metadata_legacy._classify(conn, 'enrichment_conflict', source)
        expected = (source['id'], header['generation'], header['filename'], header['digest'], len(encoded),
            entry, recording, video, field, scope, disposition, preparation.STATUSES[source['resolved']],
            metadata_legacy._clock(source['updated_at']), kind, len(candidates))
        if tuple(header) != expected:
            raise MigrationError('Prepared conflict header differs from its source or references')
        key = (header['conflict_id'], header['generation'])
        compat = conn.execute('SELECT * FROM '+preparation.COMPATIBILITY+' WHERE conflict_id=? AND generation=?', key).fetchone()
        if compat is None or tuple(compat) != (*key, *preparation.compatibility(source, kind)):
            raise MigrationError('Prepared conflict compatibility fields differ from their source')
        reader = conn.execute('SELECT * FROM '+preparation.CANDIDATES+
            ' WHERE conflict_id=? AND generation=? ORDER BY position', key)
        for position, candidate in enumerate(candidates):
            raw = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
            actual = reader.fetchone()
            expected = (*key, *preparation.describe_candidate(conn, position, raw, candidate, field, entry))
            if actual is None or tuple(actual) != expected:
                raise MigrationError('Prepared conflict candidate differs from its ordered source or references')
        if reader.fetchone() is not None:
            raise MigrationError('Prepared conflict contains extra candidates')
        resolution = conn.execute('SELECT * FROM '+preparation.RESOLUTIONS+
            ' WHERE conflict_id=? AND generation=?', key).fetchone()
        expected = preparation.describe_resolution(conn, source, field, entry)
        if (tuple(resolution) if resolution is not None else None) != ((*key, *expected) if expected is not None else None):
            raise MigrationError('Prepared saved conflict decision differs from its source')
    finally:
        conn.rollback()


class ConflictPublication(MappedPublication):
    def definitions(self):
        yield from super().definitions()
        yield VIEW, f'''CREATE VIEW {VIEW} AS
            SELECT g.conflict_id AS id,c.track_id,c.field,
                CASE WHEN g.body_kind='list' THEN
                    (SELECT json_group_array(json(raw_candidate)) FROM
                        (SELECT raw_candidate FROM {preparation.CANDIDATES} p
                         WHERE p.conflict_id=g.conflict_id AND p.generation=g.generation ORDER BY p.position))
                    ELSE c.non_list_body END AS candidates,
                CASE g.status WHEN 'user_resolved' THEN 1 WHEN 'provider_converged' THEN 2 ELSE 0 END AS resolved,
                c.resolved_value,g.updated_at,c.recording_id,c.input_video_id,c.field_scope
            FROM {self.table('head')} h JOIN {self.table('map')} m ON m.stage=h.revision
            JOIN {preparation.HEAD} g ON g.conflict_id=m.source AND g.generation=m.generation
            JOIN {preparation.COMPATIBILITY} c USING(conflict_id,generation)
            WHERE h.name={self.name!r} AND h.active=1'''

    def step(self, conn, *, batch_size=16):
        if type(batch_size) is not int or not 1 <= batch_size <= 16:
            raise ValueError('Conflict publication batches require 1 to 16 links')
        return super().step(conn, batch_size=batch_size)


# Additive compatibility captures share the same compound custody key. Include
# them in the immutable map guards without changing the installed preparation
# definitions or rewriting any previously prepared generation.
_job = replace(preparation.DEFINITION,
    destinations=preparation.DEFINITION.destinations+preparation.DEFINITION.additive_destinations,
    additive_destinations=())
OWNER = ConflictPublication('metadata-conflicts', SimpleNamespace(definition=_job, progress=preparation.JOB.progress),
    preparation.HEAD, preparation.CURRENT, metadata_evidence.available, verify_document, preparation.read_original,
    source_column='conflict_id', generation_column='generation')


def table(conn):
    return VIEW if OWNER.selected(conn) is not None else 'enrichment_conflict'


def repair(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Conflict file recovery requires an idle connection')
    header = conn.execute('SELECT g.* FROM '+preparation.CURRENT+' h JOIN '+preparation.HEAD+
        ' g USING(conflict_id,generation) WHERE conflict_id=?', (identity,)).fetchone()
    source = preparation._source(conn, identity)
    if header is None or source is None:
        raise MigrationError('Conflict original is missing; reconcile before recovery')
    _, encoded, _, _ = preparation._document(source)
    digest = hashlib.sha256(encoded).hexdigest()
    if digest != header['digest'] or len(encoded) != header['byte_size']:
        raise MigrationError('Conflict changed; reconcile before file recovery')
    if str(_publish_encoded(preparation.directory(conn), encoded, digest)) != header['filename']:
        raise MigrationError('Rebuilt conflict has a different original path')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-conflict', type=int)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=16)
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
                print(json.dumps({'published': args.publish_stage}))
            elif args.repair_conflict is not None:
                repair(conn, args.repair_conflict)
                print(json.dumps({'repaired': args.repair_conflict}))
            else:
                for _ in range(args.batches):
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    print(json.dumps(stage.__dict__))
                    if stage.complete:
                        break
        return 0
    except (OSError, ValueError, sqlite3.Error, MigrationError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    raise SystemExit(main())
