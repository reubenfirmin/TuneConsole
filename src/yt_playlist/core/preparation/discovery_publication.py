"""Retain coordinated canonical discovery inputs before selecting their views."""
from dataclasses import asdict, dataclass
import hashlib
import json
import time

from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import bounded, discovery_artists, discovery_artist_publication, discovery_subjects as preparation, discovery_views


INPUTS = {'scans': preparation.SCANS, 'sources': preparation.SOURCES,
    'artist_sources': preparation.ARTISTS, 'impressions': preparation.IMPRESSIONS, 'memberships': preparation.CANDIDATES,
    'classifications': preparation.CLASSIFICATIONS}
MAX_ROW_BYTES = 4*preparation.MAX_ROW_BYTES


def origin_key(row):
    return canonical([row[field] for field in discovery_views.ORIGIN_FIELDS
                      if field not in ('resolution_status', 'resolution_reason')]).decode()


@dataclass(frozen=True)
class Stage:
    id: int
    proof: str
    owner: str
    after_key: str | None
    copied: int
    complete: int


@dataclass(frozen=True)
class Publication:
    name: str

    @property
    def artist(self):
        return discovery_artist_publication.OWNERS['artists_'+self.name]

    @property
    def kinds(self):
        return (*tuple(kind for kind in INPUTS if kind != 'classifications'), 'artists',
                *(('classifications',) if self.name == 'classified' else ()))

    def table(self, suffix):
        return 'published_discovery_'+self.name+'_'+suffix

    @property
    def jobs(self):
        return (preparation.JOB, *((preparation.CLASSIFICATION_JOB,) if self.name == 'classified' else ()))

    def definitions(self):
        yield self.table('epoch'), 'CREATE TABLE '+self.table('epoch')+' (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
        yield self.table('stages'), ('CREATE TABLE '+self.table('stages')+' (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'proof TEXT NOT NULL CHECK(json_valid(proof)),owner TEXT NOT NULL CHECK(owner IN '+repr(self.kinds)+'),'
            'after_key TEXT CHECK(after_key IS NULL OR json_valid(after_key)),copied INTEGER NOT NULL CHECK(copied>=0),'
            'complete INTEGER NOT NULL CHECK(complete IN (0,1)))')
        specifications = dict(preparation.definitions())
        specifications.update(preparation.CLASSIFICATION_JOB.destinations)
        for kind in self.kinds:
            if kind == 'artists':
                sql = dict(discovery_artists.definitions(self.artist.name))[self.artist.header]
                body = sql.split('(', 1)[1].rsplit(')', 1)[0]
                body = body.replace('PRIMARY KEY(source,preparation_revision)', 'PRIMARY KEY(stage,source)')
                extra = ('source_ordinal INTEGER NOT NULL,' if kind == 'artists' else '')
                body += ',UNIQUE(stage,source_ordinal),FOREIGN KEY(source,preparation_revision) REFERENCES '+self.artist.header+'(source,preparation_revision)'
            else:
                sql = specifications[INPUTS[kind]]
                body = sql.split('(', 1)[1].rsplit(')', 1)[0]
                body = body.replace('PRIMARY KEY(source_owner,source_key)', 'PRIMARY KEY(stage,source_owner,source_key)')
                extra = ('origin_key TEXT NOT NULL CHECK(json_valid(origin_key)),' if kind in ('sources', 'artist_sources') else '')
                extra += ('source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot) '
                          'AND length(CAST(source_snapshot AS BLOB))<='+str(preparation.MAX_ROW_BYTES)+'),')
            yield self.table('input_'+kind), ('CREATE TABLE '+self.table('input_'+kind)+' (stage INTEGER NOT NULL REFERENCES '+
                self.table('stages')+'(id),'+extra+body+') WITHOUT ROWID')
        indexes = {'scans': ('artist_id',), 'sources': ('recording_id,origin_key,input_video_id', 'origin_key,recording_id'),
            'artist_sources': ('subject_artist_id,origin_key',),
            'impressions': ('recording_id,view_id', 'artist_id,view_id', 'album_id,view_id'),
            'memberships': ('kind,recording_id,album_id',), 'artists': ('artist_id,source_ordinal',)}
        for kind, fields in indexes.items():
            for index, columns in enumerate(fields):
                name = self.table(kind+'_subject_'+str(index))
                yield name, 'CREATE INDEX '+name+' ON '+self.table('input_'+kind)+'(stage,'+columns+')'
        yield self.table('generations'), ('CREATE TABLE '+self.table('generations')+
            ' (revision INTEGER PRIMARY KEY REFERENCES '+self.table('stages')+'(id),proof TEXT NOT NULL CHECK(json_valid(proof)),published_at REAL NOT NULL)')
        yield self.table('head'), ('CREATE TABLE '+self.table('head')+
            ' (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES '+self.table('generations')+
            '(revision),active INTEGER NOT NULL CHECK(active IN (0,1)))')
        for suffix in (*('input_'+kind for kind in self.kinds), 'generations'):
            for operation in ('UPDATE', 'DELETE'):
                name = self.table(suffix+'_'+operation.lower())
                yield name, ('CREATE TRIGGER '+name+' BEFORE '+operation+' ON main.'+self.table(suffix)+
                    " BEGIN SELECT RAISE(ABORT,'Retained discovery publication requires future cleanup'); END")
            if suffix.startswith('input_'):
                name = self.table(suffix+'_insert')
                yield name, ('CREATE TRIGGER '+name+' BEFORE INSERT ON main.'+self.table(suffix)+
                    ' WHEN EXISTS(SELECT 1 FROM '+self.table('stages')+' WHERE id=NEW.stage AND complete=1) '
                    "BEGIN SELECT RAISE(ABORT,'Completed discovery publication is retained'); END")
        for operation in ('UPDATE', 'DELETE'):
            name = self.table('stages_'+operation.lower())
            yield name, ('CREATE TRIGGER '+name+' BEFORE '+operation+' ON main.'+self.table('stages')+
                " WHEN OLD.complete=1 BEGIN SELECT RAISE(ABORT,'Completed discovery stages are retained'); END")
        dependencies = [*dict.fromkeys(INPUTS[kind] for kind in self.kinds if kind != 'artists'),
                        preparation.ORIGINALS, 'catalog_entries', self.artist.table('head'), self.artist.table('epoch')]
        if self.name == 'classified':
            dependencies.append(preparation.CLASSIFICATION_ORIGINALS)
        for dependency in dependencies:
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = self.table('dependency_'+dependency+'_'+operation.lower())
                yield name, ('CREATE TRIGGER '+name+' AFTER '+operation+' ON main.'+dependency+' BEGIN '
                    'UPDATE '+self.table('epoch')+' SET revision=revision+1 WHERE id=1; '
                    'UPDATE '+self.table('head')+' SET active=0 WHERE active=1; END')
        for job in self.jobs:
            name = self.table('input_'+job.name)
            yield name, ('CREATE TRIGGER '+name+' AFTER UPDATE ON main.'+job.state+
                ' WHEN NEW.input_revision<>OLD.input_revision BEGIN UPDATE '+self.table('head')+' SET active=0 WHERE active=1; END')
        for name, query in discovery_views.definitions(self.table, classified=self.name == 'classified'):
            yield self.table(name), 'CREATE VIEW '+self.table(name)+' AS '+query

    def validate(self, conn):
        from .. import genre_classifications
        from ..schema_contract import tokens as _tokens
        from ..genre_contract import classified_definition as classified
        from ..entity_names import predecessor_definition as transform
        renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone()
        native = discovery_artist_publication.native(conn)
        classified_schema = self.name == 'legacy' and genre_classifications.available(conn)
        for name, sql in self.definitions():
            if native and sql.startswith('CREATE VIEW'):
                continue
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if classified_schema:
                sql = classified(sql)
            if row is None or _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
                raise MigrationError('Incompatible discovery publication object: '+name)
        if not conn.execute('SELECT 1 FROM '+self.table('epoch')+' WHERE id=1').fetchone():
            raise MigrationError('Discovery publication requires its dependency revision')

    def validate_installed(self, conn):
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return
        self.validate(conn)
        if not discovery_artist_publication.native(conn):
            from ..schema_contract import tokens as _tokens
            for job in self.jobs:
                # A retained publication from an earlier format can predate a
                # newly introduced optional owner. It is no longer selectable;
                # the explicit worker installs capture and invalidates proof
                # before the new format can publish.
                optional = ({'prepare_'+job.name+'_dependency_'+table+'_'+operation
                    for table in job.optional_dependencies for operation in ('insert', 'update', 'delete')}
                    if self.name != owner(conn).name else set())
                for name, sql in job.definitions(conn):
                    row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
                    if row is None and name in optional:
                        continue
                    if row is None or _tokens(row[0]) != _tokens(sql):
                        raise MigrationError('Incompatible discovery preparation object: '+name)
            self.artist.validate_installed(conn)
        if conn.execute('SELECT 1 FROM '+self.table('head')+' h LEFT JOIN '+self.table('generations')+
            ' g ON g.revision=h.revision LEFT JOIN '+self.table('stages')+
            ' s ON s.id=g.revision WHERE g.revision IS NULL OR s.id IS NULL OR s.complete<>1 OR s.proof<>g.proof LIMIT 1').fetchone():
            raise MigrationError('Discovery selection requires its completed stage')

    def install(self, conn):
        if conn.in_transaction:
            raise MigrationError('Discovery publication requires an idle connection')
        def installed():
            for job in self.jobs:
                job._version(conn)
                job.validate(conn)
            self.artist.job._version(conn)
            self.artist.validate_installed(conn)
            if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.artist.table('head'),)).fetchone():
                raise MigrationError('Discovery publication requires retained artist file publication')
            exists = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone()
            if exists:
                self.validate(conn)
            return exists
        try:
            # Repeated stage batches only inspect existing definitions. Hold a
            # read snapshot for that work; reserving a writer on every check can
            # starve serving writes even when individual copy batches are short.
            conn.execute('BEGIN')
            exists = installed()
            conn.rollback()
            if exists:
                return
            conn.execute('BEGIN IMMEDIATE')
            if not installed():
                for _, sql in self.definitions():
                    conn.execute(sql)
                conn.execute('INSERT INTO '+self.table('epoch')+' VALUES (1,0)')
            conn.commit()
        finally:
            conn.rollback()

    def proof(self, conn):
        progress = []
        for job in self.jobs:
            current = job.progress(conn)
            if (current is None or current.status != 'prepared' or current.input_revision != current.prepared_revision
                    or conn.execute('SELECT 1 FROM '+job.changes+' LIMIT 1').fetchone()):
                raise MigrationError('Discovery publication requires reconciled preparation')
            progress.append(asdict(current))
        artist_revision = self.artist.selected(conn)
        if artist_revision is None:
            raise MigrationError('Discovery publication requires selected artist file custody')
        artist_proof = self.artist.proof(conn)
        epoch = conn.execute('SELECT revision FROM '+self.table('epoch')+' WHERE id=1').fetchone()[0]
        return canonical([progress, artist_revision, asdict(artist_proof[0]), artist_proof[1], epoch]).decode()

    def stage(self, conn, identity):
        row = conn.execute('SELECT * FROM '+self.table('stages')+' WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise MigrationError('Discovery publication stage is missing')
        return Stage(*row)

    def latest(self, conn):
        row = conn.execute('SELECT * FROM '+self.table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
        return Stage(*row) if row else None

    def batch(self, conn, kind, after, limit):
        after = json.loads(after) if after is not None else ()
        if kind == 'artists':
            query = ('SELECT h.*,d.rowid source_ordinal FROM '+self.artist.table('map')+' m JOIN '+self.artist.header+
                ' h ON h.source=m.source AND h.preparation_revision=m.generation JOIN main.discovered_artists d ON d.artist=h.source '
                'WHERE m.stage=?'+(' AND m.source>?' if after else '')+' ORDER BY m.source LIMIT ?')
            parameters = (self.artist.selected(conn), *after, limit)
        else:
            originals = preparation.CLASSIFICATION_ORIGINALS if kind == 'classifications' else preparation.ORIGINALS
            query = ('SELECT p.*,o.snapshot source_snapshot FROM '+INPUTS[kind]+' p LEFT JOIN '+originals+
                ' o USING(source_owner,source_key,source_digest)'+
                (' WHERE (p.source_owner,p.source_key)>(?,?)' if after else '')+' ORDER BY p.source_owner,p.source_key LIMIT ?')
            parameters = (*after, limit)
        records = bounded.rows(conn, query, parameters, byte_limit=MAX_ROW_BYTES, message='Discovery publication row exceeds its byte budget')
        for row in records:
            if kind in ('sources', 'artist_sources'):
                row['origin_key'] = origin_key(row)
            if len(canonical(row)) > MAX_ROW_BYTES:
                raise MigrationError('Discovery publication row exceeds its byte budget')
        return records

    def references(self, conn, kind, row):
        fields = []
        if kind in ('sources', 'artist_sources'):
            fields += [('account_id', 'identities', 'id'), ('album_id', 'albums', 'id'),
                       ('seed_video_id', 'youtube_items', 'video_id'), ('genre_key', 'genre_catalog', 'key'),
                       ('artist_id', 'artists', 'id')]
        if kind == 'sources':
            fields += [('recording_id', 'recordings', 'id'), ('input_video_id', 'youtube_items', 'video_id')]
        elif kind == 'artist_sources':
            fields.append(('subject_artist_id', 'artists', 'id'))
        elif kind in ('impressions', 'classifications'):
            fields += [('recording_id', 'recordings', 'id'), ('album_id', 'albums', 'id'), ('artist_id', 'artists', 'id')]
            if kind == 'classifications':
                fields.append(('genre_key', 'genre_catalog', 'key'))
        elif kind == 'scans':
            fields.append(('artist_id', 'artists', 'id'))
        for field, table, key in fields:
            if row[field] is not None and not conn.execute('SELECT 1 FROM main.'+table+' WHERE '+key+'=?', (row[field],)).fetchone():
                raise MigrationError('Discovery publication has an invalid reference: '+field)

    def step(self, conn, *, batch_size=16):
        if type(batch_size) is not int or not 1 <= batch_size <= 16:
            raise ValueError('Discovery publication batches require 1 to 16 rows')
        self.install(conn)
        try:
            conn.execute('BEGIN')
            captured, previous = self.proof(conn), self.latest(conn)
            current = previous if previous and previous.proof == captured else Stage(0, captured, self.kinds[0], None, 0, 0)
            if current.complete:
                return current
            records = self.batch(conn, current.owner, current.after_key, batch_size+1)
            more, records = len(records)>batch_size, records[:batch_size]
            conn.rollback()
            conn.execute('BEGIN IMMEDIATE')
            if self.proof(conn) != captured or self.latest(conn) != previous:
                raise MigrationError('Discovery inputs changed before staging; reconcile again')
            if not current.id:
                identity = conn.execute('INSERT INTO '+self.table('stages')+
                    '(proof,owner,copied,complete) VALUES (?,?,0,0)', (captured, current.owner)).lastrowid
                current = self.stage(conn, identity)
            for row in records:
                conn.execute('INSERT INTO '+self.table('input_'+current.owner)+'(stage,'+','.join(row)+') VALUES ('+
                    ','.join('?' for _ in range(len(row)+1))+')', (current.id, *row.values()))
            next_kind = current.owner if more or current.owner == self.kinds[-1] else self.kinds[self.kinds.index(current.owner)+1]
            keys = ('source',) if current.owner == 'artists' else ('source_owner', 'source_key')
            cursor = json.dumps([records[-1][field] for field in keys], ensure_ascii=False) if more else None
            conn.execute('UPDATE '+self.table('stages')+' SET owner=?,after_key=?,copied=?,complete=? WHERE id=?',
                (next_kind, cursor, current.copied+len(records), int(current.owner == self.kinds[-1] and not more), current.id))
            result = self.stage(conn, current.id)
            conn.commit()
            return result
        finally:
            conn.rollback()

    def verify(self, conn, identity):
        if conn.in_transaction:
            raise MigrationError('Discovery verification requires an idle connection')
        try:
            conn.execute('BEGIN')
            self.validate_installed(conn)
            captured, current = self.proof(conn), self.stage(conn, identity)
            if not current.complete or current.proof != captured:
                raise MigrationError('Discovery publication stage is incomplete or changed')
            artist_revision = self.artist.selected(conn)
        finally:
            conn.rollback()
        self.artist.verify(conn, artist_revision)
        total = 0
        for kind in self.kinds:
            after, count = None, 0
            while True:
                try:
                    conn.execute('BEGIN')
                    if self.proof(conn) != captured:
                        raise MigrationError('Discovery inputs changed during verification; reconcile again')
                    expected = self.batch(conn, kind, after, 16)
                    if not expected:
                        break
                    keys = ('source',) if kind == 'artists' else ('source_owner', 'source_key')
                    for row in expected:
                        actual = conn.execute('SELECT * FROM '+self.table('input_'+kind)+' WHERE stage=? AND '+
                            ' AND '.join(field+'=?' for field in keys), (identity, *(row[field] for field in keys))).fetchone()
                        if actual is None or {key: actual[key] for key in row} != row:
                            raise MigrationError('Discovery publication differs from its prepared input: '+kind)
                        if kind == 'artists':
                            continue
                        job = preparation.CLASSIFICATION_JOB if kind == 'classifications' else preparation.JOB
                        source = job._read(conn, row['source_owner'], row['source_key'])
                        if source is None:
                            raise MigrationError('Discovery publication has no installed source')
                        if kind == 'classifications':
                            original = {key: value.decode() if isinstance(value, bytes) else value for key, value in dict(source).items()}
                            raw = canonical(original).decode()
                            expected_fields = {**{key: row[key] for key in ('source_owner', 'source_key')},
                                               'source_digest': hashlib.sha256(raw.encode()).hexdigest(), **original}
                        else:
                            _, expected_fields, raw = preparation.describe(conn, row['source_owner'], row['source_key'], source)
                        if row['source_snapshot'] != raw or any(row[key] != value for key, value in expected_fields.items()):
                            raise MigrationError('Discovery publication original differs from its installed source')
                        self.references(conn, kind, row)
                        if kind == 'memberships':
                            if row['kind'] == 'track':
                                valid = conn.execute('SELECT 1 FROM main.catalog_entries WHERE id=? AND recording_id=?',
                                    (row['entry_id'], row['recording_id'])).fetchone()
                            else:
                                valid = conn.execute('SELECT 1 FROM main.albums WHERE id=?', (row['album_id'],)).fetchone()
                            if not valid:
                                raise MigrationError('Discovery publication has an invalid representative')
                    count += len(expected)
                    after = json.dumps([expected[-1][field] for field in keys], ensure_ascii=False)
                finally:
                    conn.rollback()
            if conn.execute('SELECT count(*) FROM '+self.table('input_'+kind)+' WHERE stage=?', (identity,)).fetchone()[0] != count:
                raise MigrationError('Discovery publication has missing or extra inputs: '+kind)
            if kind == 'artists':
                installed_count = conn.execute('SELECT count(*) FROM main.discovered_artists').fetchone()[0]
            else:
                owners = [owner for owner, target in preparation.TARGETS.items() if target == INPUTS[kind]]
                if kind == 'classifications':
                    owners = ['genre_classification_legacy']
                installed_count = sum(conn.execute('SELECT count(*) FROM main.'+owner).fetchone()[0] for owner in owners)
            if installed_count != count:
                raise MigrationError('Discovery publication omits installed source rows: '+kind)
            total += count
        if total != current.copied:
            raise MigrationError('Discovery publication has an incorrect retained row count')
        try:
            conn.execute('BEGIN')
            if (self.proof(conn), self.stage(conn, identity)) != (captured, current):
                raise MigrationError('Discovery inputs changed during verification; reconcile again')
        finally:
            conn.rollback()
        return captured, current

    def publish(self, conn, *, stage, clock=time.time):
        if type(stage) is not int or stage <= 0:
            raise ValueError('Discovery publication requires a positive stage identity')
        self.install(conn)
        captured = self.verify(conn, stage)
        try:
            conn.execute('BEGIN IMMEDIATE')
            if (self.proof(conn), self.stage(conn, stage)) != captured:
                raise MigrationError('Discovery inputs changed after verification; reconcile again')
            conn.execute('INSERT INTO '+self.table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING', (stage, captured[0], clock()))
            conn.execute('INSERT INTO '+self.table('head')+' VALUES (1,?,1) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1', (stage,))
            conn.commit()
        finally:
            conn.rollback()

    def selected(self, conn):
        if discovery_artist_publication.native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+self.table('head')+' WHERE active=1').fetchone()
        return row[0] if row else None


OWNERS = {name: Publication(name) for name in ('legacy', 'classified')}


def owner(conn):
    return OWNERS['classified' if discovery_artist_publication.owner(conn).name == 'artists_classified' else 'legacy']


@dataclass(frozen=True)
class ReadOwners:
    """Capture one publication for a complete read, without changing SQL writers."""
    conn: object
    publication: Publication
    revision: int | None
    native: bool

    @property
    def typed(self):
        return self.native or self.revision is not None

    def relation(self, name):
        if self.revision is not None:
            names = {name for name, _ in discovery_views.definitions(self.publication.table,
                classified=self.publication.name == 'classified')}
            if name not in names:
                raise ValueError('Unknown discovery read owner: '+name)
            # Private genre views compose with this pinned discovery selection.
            scope = '' if name in ('discovered_tracks', 'discovered_albums') else 'main.'
            return '(SELECT * FROM '+scope+self.publication.table(name)+' WHERE stage='+str(self.revision)+')'
        if name == 'discovered_artists':
            return discovery_artist_publication.relation(self.conn)
        return name if name in ('discovered_tracks', 'discovered_albums') else 'main.'+name

    def installed_artist_ids(self, canonical_ids):
        """Resolve deletion targets before the first write withdraws the head."""
        if self.revision is None:
            return set(canonical_ids)
        return {row[0] for aid in canonical_ids for row in self.conn.execute(
            'SELECT captured_artist_id FROM '+self.publication.table('input_artists')+
            ' WHERE stage=? AND artist_id=? AND captured_artist_id IS NOT NULL', (self.revision, aid))}


def read_owners(conn):
    from .. import discovery_provenance
    publication = owner(conn)
    return ReadOwners(conn, publication, publication.selected(conn), discovery_provenance.available(conn))


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
    parser.add_argument('--publish-stage', type=int)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=16)
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
            else:
                for _ in range(args.batches):
                    stage = publication.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Discovery publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
