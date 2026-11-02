"""Publish retained delivery ranges and explicit metadata lineage on installed v79.

The compatible catalog keeps its typed base values. Historical provider labels
remain unresolved attribution; equal values never select an observation. Files
are prepared outside SQL transactions, and source writers withdraw the selected
read family through the existing preparation revision.
"""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time

from .. import metadata_delivery_files as files, metadata_legacy, metadata_native
from ..activity import WorkBudgetExceeded
from ..db_revisions import keyed_reads
from ..schema_contract import MigrationError
from . import metadata_evidence as preparation, metadata_publication as publication


PREFIX = 'published_metadata_destination_'


def table(name):
    return PREFIX+name


@dataclass(frozen=True)
class Stage:
    id: int
    proof: str
    after_owner: str | None
    after_key: str | None
    copied: int
    complete: int


def definitions():
    yield table('stages'), f'''CREATE TABLE {table('stages')} (
        id INTEGER PRIMARY KEY REFERENCES {publication.table('stages')}(id),
        proof TEXT NOT NULL CHECK(json_valid(proof)),after_owner TEXT,after_key TEXT,
        copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
    yield table('deliveries'), f'''CREATE TABLE {table('deliveries')} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,source_key TEXT NOT NULL,source_digest TEXT NOT NULL,
        provider TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT,observed_at REAL,
        filename TEXT NOT NULL,digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        byte_offset INTEGER NOT NULL CHECK(byte_offset>=0),UNIQUE(owner,source_key,source_digest),
        FOREIGN KEY(owner,source_key,source_digest) REFERENCES {preparation.ORIGINALS}(owner,source_key,digest))'''
    yield table('records'), f'''CREATE TABLE {table('records')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),owner TEXT NOT NULL,source_key TEXT NOT NULL,
        delivery_id INTEGER NOT NULL REFERENCES {table('deliveries')}(id),PRIMARY KEY(stage,owner,source_key)) WITHOUT ROWID'''
    yield table('observations'), f'''CREATE TABLE {table('observations')} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,stage INTEGER NOT NULL REFERENCES {table('stages')}(id),
        owner TEXT NOT NULL,source_key TEXT NOT NULL,role TEXT NOT NULL,position INTEGER NOT NULL CHECK(position>=0),
        delivery_id INTEGER NOT NULL REFERENCES {table('deliveries')}(id),entry_id INTEGER,recording_id INTEGER,
        input_video_id TEXT,field_scope TEXT NOT NULL,field TEXT NOT NULL CHECK(field IN {repr(tuple(metadata_legacy.CATALOG_FIELDS))}),
        value_kind TEXT NOT NULL CHECK(value_kind IN ('value','null','blank','unresolved','withdrawn')),
        text_value TEXT,number_value,genre_key TEXT,unit TEXT,resolution_reason TEXT,
        observed_at REAL,origin TEXT NOT NULL,legacy_source TEXT,source_run_id TEXT,legacy_reference TEXT,
        UNIQUE(stage,owner,source_key,role,position),
        CHECK(number_value IS NULL OR (typeof(number_value) IN ('integer','real') AND
            abs(number_value)<=1.7976931348623157e308)))'''
    yield table('observations_entry'), 'CREATE INDEX '+table('observations_entry')+' ON '+table('observations')+'(stage,entry_id,origin,id)'
    yield table('native'), f'''CREATE TABLE {table('native')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),legacy_id INTEGER NOT NULL,
        delivery_id INTEGER NOT NULL REFERENCES {table('deliveries')}(id),entry_id INTEGER,recording_id INTEGER,
        field TEXT NOT NULL,kind TEXT NOT NULL,unit TEXT,source_key TEXT,
        PRIMARY KEY(stage,legacy_id)) WITHOUT ROWID'''
    yield table('native_recording'), 'CREATE INDEX '+table('native_recording')+' ON '+table('native')+'(stage,recording_id,legacy_id)'
    yield table('native_source'), 'CREATE INDEX '+table('native_source')+' ON '+table('native')+'(stage,source_key,field,legacy_id DESC)'
    yield table('choices'), f'''CREATE TABLE {table('choices')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),entry_id INTEGER NOT NULL,field TEXT NOT NULL,
        observation_id INTEGER NOT NULL REFERENCES {table('observations')}(id),
        PRIMARY KEY(stage,entry_id,field)) WITHOUT ROWID'''
    yield table('selections'), f'''CREATE TABLE {table('selections')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),owner TEXT NOT NULL,subject INTEGER NOT NULL,field TEXT NOT NULL,
        attribution_status TEXT NOT NULL CHECK(attribution_status='legacy_unresolved'),
        legacy_source TEXT NOT NULL,selected_at REAL,
        observation_id INTEGER CHECK(observation_id IS NULL),PRIMARY KEY(stage,owner,subject,field)) WITHOUT ROWID'''
    yield table('fields'), f'''CREATE TABLE {table('fields')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),owner TEXT NOT NULL,source_key TEXT NOT NULL,field TEXT NOT NULL,position INTEGER NOT NULL,
        delivery_id INTEGER NOT NULL REFERENCES {table('deliveries')}(id),
        observation_id INTEGER REFERENCES {table('observations')}(id),PRIMARY KEY(stage,owner,source_key,field)) WITHOUT ROWID'''
    yield table('hints'), f'''CREATE TABLE {table('hints')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),entry_id INTEGER NOT NULL,field TEXT NOT NULL,source TEXT NOT NULL,
        observation_id INTEGER NOT NULL REFERENCES {table('observations')}(id),active INTEGER NOT NULL,
        PRIMARY KEY(stage,entry_id,field,source)) WITHOUT ROWID'''
    yield table('identifiers'), f'''CREATE TABLE {table('identifiers')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),legacy_id INTEGER NOT NULL,delivery_id INTEGER NOT NULL REFERENCES {table('deliveries')}(id),
        observation_id INTEGER CHECK(observation_id IS NULL),PRIMARY KEY(stage,legacy_id)) WITHOUT ROWID'''
    yield table('head'), f'''CREATE TABLE {table('head')} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {table('stages')}(id),published_at REAL NOT NULL)'''
    yield table('failures'), f'''CREATE TABLE {table('failures')} (
        stage INTEGER PRIMARY KEY REFERENCES {publication.table('stages')}(id),error TEXT NOT NULL,failed_at REAL NOT NULL)'''
    # A source cache is a current dependency projection, never proof that equal
    # catalog values share an observation. Its writes withdraw the whole family
    # and require fresh verification of the exact field references below.
    for operation in ('INSERT','UPDATE','DELETE'):
        name = table('source_'+operation.lower())
        yield name, ('CREATE TRIGGER '+name+' AFTER '+operation+' ON main.rec_musical_sources BEGIN '+
            'UPDATE '+publication.table('epoch')+' SET revision=revision+1 WHERE id=1; '+
            'UPDATE '+publication.table('head')+' SET active=0 WHERE active=1; END')
    for owner in ('deliveries','records','observations','native','choices','selections','fields','hints','identifiers','stages'):
        for operation in ('UPDATE','DELETE'):
            name = table(owner+'_'+operation.lower())
            yield name, ('CREATE TRIGGER '+name+' BEFORE '+operation+' ON main.'+table(owner)+
                (' WHEN OLD.complete=1' if owner=='stages' else '')+
                " BEGIN SELECT RAISE(ABORT,'Metadata destination custody is retained'); END")
        if owner not in ('deliveries','stages'):
            name = table(owner+'_insert')
            yield name, ('CREATE TRIGGER '+name+' BEFORE INSERT ON main.'+table(owner)+
                ' WHEN EXISTS(SELECT 1 FROM '+table('stages')+' WHERE id=NEW.stage AND complete=1) '+
                "BEGIN SELECT RAISE(ABORT,'Completed metadata destination stage is retained'); END")


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone():
        return
    from ..schema_contract import tokens as _tokens
    from ..entity_names import predecessor_definition as transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for name,sql in definitions():
        actual = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if actual is None or _tokens(actual[0])!=_tokens(transform(sql) if renamed else sql):
            raise MigrationError('Incompatible metadata destination: '+name)
    if conn.execute('SELECT 1 FROM '+table('head')+' h LEFT JOIN '+table('stages')+
            ' s ON s.id=h.revision WHERE s.id IS NULL OR s.complete<>1 LIMIT 1').fetchone():
        raise MigrationError('Metadata destination requires a complete stage')


def install(conn):
    publication.install(conn)
    try:
        conn.execute('BEGIN')
        exists = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone()
        if exists:
            validate_installed(conn)
            return
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone():
            for _,sql in definitions():
                conn.execute(sql)
        validate_installed(conn)
        conn.commit()
    finally:
        conn.rollback()


def directory(conn):
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
    return path.with_name(path.name+'.preparation')/'metadata-deliveries'


def stage(conn, identity):
    row = conn.execute('SELECT * FROM '+table('stages')+' WHERE id=?',(identity,)).fetchone()
    return Stage(*row) if row else None


def captured(conn, identity):
    proof,current = publication.proof(conn),publication.stage(conn,identity)
    if not current.complete or current.proof!=proof:
        raise MigrationError('Metadata destinations require a complete reconciled metadata stage')
    return proof


def document(row):
    return {'format':metadata_legacy.FORMAT,'owner':row['owner'],'row':json.loads(row['source_snapshot'])}


def delivery_values(row):
    native = json.loads(row['native_provenance']) if row['native_provenance'] is not None else None
    return (row['owner'],row['source_key'],row['source_digest'],row['provider'] or 'legacy_unknown',
        native['scope'] if native else 'legacy_unknown',native['entity_id'] if native else None,row['observed_at'])


def observations(conn, row):
    """Finite evidence rows; provider equality does not infer any selection."""
    source = json.loads(row['source_snapshot'])
    owner = row['owner']
    if (row['value_kind'] is not None and row['field'] in metadata_legacy.CATALOG_FIELDS
            and row['entry_id'] is not None):
        yield ('value',0,row['entry_id'],row['recording_id'],row['input_video_id'],row['field_scope'],row['field'],
            *(row[name] for name in ('value_kind','text_value','number_value','genre_key','unit','resolution_reason')),
            row['observed_at'],metadata_legacy.OBSERVATION_OWNERS.get(owner,'legacy_hint'),row['provider'],
            source.get('run_id'),source.get('legacy_ref'))
    for child in publication.operational(conn,row):
        if child['value_kind'] is not None:
            yield ('operational',child['position'],None,child['recording_id'],child['input_video_id'],'legacy_recording',
                child['canonical_field'],*(child[name] for name in ('value_kind','text_value','number_value','genre_key','unit','resolution_reason')),
                row['observed_at'],'legacy_attempt' if owner=='provider_attempts' else 'legacy_state',row['provider'],None,None)


def source_key(conn, row):
    if row['native_provenance'] is None:
        return None
    native = json.loads(row['native_provenance'])
    if native['scope'] not in ('recording','release','artist'):
        return None
    key = json.dumps([row['provider'],native['scope'],native['entity_id']],ensure_ascii=False,separators=(',',':'))
    return key if conn.execute('SELECT 1 FROM rec_musical_sources WHERE source_key=?',(key,)).fetchone() else None


def insert_record(conn, identity, row, delivery):
    owner,key,source = row['owner'],row['source_key'],json.loads(row['source_snapshot'])
    conn.execute('INSERT INTO '+table('records')+' VALUES (?,?,?,?)',(identity,owner,key,delivery))
    found = {}
    for value in observations(conn,row):
        oid = conn.execute('INSERT INTO '+table('observations')+'(stage,owner,source_key,role,position,delivery_id,'+
            'entry_id,recording_id,input_video_id,field_scope,field,value_kind,text_value,number_value,genre_key,unit,'+
            'resolution_reason,observed_at,origin,legacy_source,source_run_id,legacy_reference) VALUES ('+
            ','.join('?' for _ in range(22))+')',(identity,owner,key,*value[:2],delivery,*value[2:])).lastrowid
        found[value[:2]] = oid
    if owner=='entry_field_choices':
        if ('value',0) not in found or row['value_kind']=='unresolved':
            raise MigrationError('Active metadata choice requires its retained observation')
        conn.execute('INSERT INTO '+table('choices')+' VALUES (?,?,?,?)',(identity,row['entry_id'],row['field'],found['value',0]))
    elif owner in ('recording_field_sources','item_field_sources'):
        conn.execute('INSERT INTO '+table('selections')+" VALUES (?,?,?,?,'legacy_unresolved',?,?,NULL)",
            (identity,owner,source.get('recording_id',source.get('entry_id')),row['field'],source['source'],source['selected_at']))
    elif owner=='catalog_field_hints' and ('value',0) in found:
        conn.execute('INSERT INTO '+table('hints')+' VALUES (?,?,?,?,?,?)',
            (identity,row['entry_id'],row['field'],source['source'],found['value',0],source['active']))
    elif owner=='recording_identifiers':
        conn.execute('INSERT INTO '+table('identifiers')+' VALUES (?,?,?,NULL)',(identity,source['id'],delivery))
    if owner=='metadata_findings' and source['provenance_json'] is not None:
        native = json.loads(row['native_provenance']) if row['native_provenance'] is not None else {}
        conn.execute('INSERT INTO '+table('native')+' VALUES (?,?,?,?,?,?,?,?,?)',
            (identity,source['id'],delivery,row['entry_id'],row['recording_id'],source['field'],native.get('kind','legacy_unknown'),native.get('unit'),source_key(conn,row)))
    if owner in ('provider_attempts','enrichment_provider_state'):
        for child in publication.operational(conn,row):
            conn.execute('INSERT INTO '+table('fields')+' VALUES (?,?,?,?,?,?,?)',
                (identity,owner,key,child['field'],child['position'],delivery,found.get(('operational',child['position']))))


def step(conn, *, identity, batch_size=16, retry=False):
    if type(batch_size) is not int or not 1<=batch_size<=16:
        raise ValueError('Metadata destinations require 1 to 16 records per batch')
    install(conn)
    record_failure = False
    try:
        conn.execute('BEGIN')
        failed = conn.execute('SELECT error FROM '+table('failures')+' WHERE stage=?',(identity,)).fetchone()
        if failed and not retry:
            raise MigrationError('Repair the metadata destination input and retry explicitly: '+failed[0])
        proof,previous = captured(conn,identity),stage(conn,identity)
        record_failure = True
        current = previous or Stage(identity,proof,None,None,0,0)
        if current.proof!=proof:
            raise MigrationError('Metadata destination stage changed; prepare a reconciled source stage')
        if current.complete:
            if failed:
                conn.rollback()
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM '+table('failures')+' WHERE stage=?',(identity,))
                conn.commit()
            return current
        rows = publication.batch(conn,current,batch_size+1)
        more,rows = len(rows)>batch_size,rows[:batch_size]
        reused = [conn.execute('SELECT * FROM '+table('deliveries')+' WHERE owner=? AND source_key=? AND source_digest=?',
            (row['owner'],row['source_key'],row['source_digest'])).fetchone() for row in rows]
        conn.rollback()
        references = []
        for row,old in zip(rows,reused,strict=True):
            if old:
                if files.read(old['filename'],old['digest'],old['byte_size'],old['byte_offset'])!=document(row):
                    raise MigrationError('Metadata delivery original differs from its captured source')
                references.append(tuple(old[name] for name in ('filename','digest','byte_size','byte_offset')))
            else:
                references.append(None)
        pending = iter(files.publish_encoded(directory(conn),(files.encode(document(row))
            for row,old in zip(rows,reused,strict=True) if old is None)))
        references = [value if value is not None else next(pending) for value in references]
        conn.execute('BEGIN IMMEDIATE')
        if captured(conn,identity)!=proof or stage(conn,identity)!=previous:
            raise MigrationError('Metadata source or destination checkpoint changed before commit')
        if previous is None:
            conn.execute('INSERT INTO '+table('stages')+' VALUES (?,?,NULL,NULL,0,0)',(identity,proof))
        for row,reference in zip(rows,references,strict=True):
            values = delivery_values(row)
            conn.execute('INSERT INTO '+table('deliveries')+'(owner,source_key,source_digest,provider,entity_type,entity_id,'+
                'observed_at,filename,digest,byte_size,byte_offset) VALUES ('+','.join('?' for _ in range(11))+') ON CONFLICT DO NOTHING',
                (*values,*reference))
            saved = conn.execute('SELECT * FROM '+table('deliveries')+' WHERE owner=? AND source_key=? AND source_digest=?',values[:3]).fetchone()
            if tuple(saved)[1:]!=(*values,*reference):
                raise MigrationError('Concurrent metadata delivery has a different retained reference; retry the batch')
            insert_record(conn,identity,row,saved['id'])
        conn.execute('UPDATE '+table('stages')+' SET after_owner=?,after_key=?,copied=?,complete=? WHERE id=?',
            (rows[-1]['owner'] if rows else current.after_owner,rows[-1]['source_key'] if rows else current.after_key,
             current.copied+len(rows),int(not more),identity))
        result = stage(conn,identity)
        conn.execute('DELETE FROM '+table('failures')+' WHERE stage=?',(identity,))
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        # A failed file/row batch has its own durable recovery state; committed
        # destinations and ordinary regular changes remain independently usable.
        if record_failure:
            record_error(conn,identity,exc)
        raise
    finally:
        conn.rollback()


def verify(conn, identity):
    # Reuse the independent source/reference/coverage audit, never a row count
    # or a successful file write as proof of correctness.
    captured_proof = publication.verify(conn,identity)
    validate_installed(conn)
    if conn.execute('SELECT 1 FROM '+table('failures')+' WHERE stage=?',(identity,)).fetchone():
        raise MigrationError('Metadata destination has a failed batch; repair and retry explicitly')
    current = stage(conn,identity)
    if current is None or not current.complete or current.proof!=captured_proof[0]:
        raise MigrationError('Metadata destination stage is incomplete')
    cursor,count = Stage(identity,current.proof,None,None,0,0),0
    expected_counts = dict.fromkeys(('observations','native','choices','selections','fields','hints','identifiers'),0)
    while rows := publication.batch(conn,cursor):
        for row in rows:
            owner,key = row['owner'],row['source_key']
            delivery = conn.execute('SELECT d.* FROM '+table('records')+' r JOIN '+table('deliveries')+
                ' d ON d.id=r.delivery_id WHERE r.stage=? AND r.owner=? AND r.source_key=?',(identity,owner,key)).fetchone()
            if (delivery is None or tuple(delivery)[1:8]!=delivery_values(row)
                    or files.read(delivery['filename'],delivery['digest'],delivery['byte_size'],delivery['byte_offset'])!=document(row)):
                raise MigrationError('Metadata delivery differs from its source, references or original file')
            expected = list(observations(conn,row))
            actual = conn.execute('SELECT * FROM '+table('observations')+' WHERE stage=? AND owner=? AND source_key=? ORDER BY role,position',
                (identity,owner,key)).fetchall()
            if [tuple(value)[4:6]+tuple(value)[7:] for value in actual]!=sorted(expected,key=lambda value:value[:2]) or any(
                    value['delivery_id']!=delivery['id'] for value in actual):
                raise MigrationError('Metadata observations differ from their exact source or delivery')
            expected_counts['observations'] += len(expected)
            by_role = {(value['role'],value['position']):value['id'] for value in actual}
            source = json.loads(row['source_snapshot'])
            expected_relations = {name:[] for name in expected_counts if name!='observations'}
            if owner=='entry_field_choices':
                expected_relations['choices'] = [(identity,row['entry_id'],row['field'],by_role.get(('value',0)))]
            elif owner in ('recording_field_sources','item_field_sources'):
                expected_relations['selections'] = [(identity,owner,source.get('recording_id',source.get('entry_id')),
                    row['field'],'legacy_unresolved',source['source'],source['selected_at'],None)]
            elif owner=='catalog_field_hints' and ('value',0) in by_role:
                expected_relations['hints'] = [(identity,row['entry_id'],row['field'],source['source'],by_role['value',0],source['active'])]
            elif owner=='recording_identifiers':
                expected_relations['identifiers'] = [(identity,source['id'],delivery['id'],None)]
            if owner=='metadata_findings' and source['provenance_json'] is not None:
                native = json.loads(row['native_provenance']) if row['native_provenance'] is not None else {}
                expected_relations['native'] = [(identity,source['id'],delivery['id'],row['entry_id'],row['recording_id'],
                    source['field'],native.get('kind','legacy_unknown'),native.get('unit'),source_key(conn,row))]
            if owner in ('provider_attempts','enrichment_provider_state'):
                expected_relations['fields'] = [(identity,owner,key,child['field'],child['position'],delivery['id'],by_role.get(('operational',child['position'])))
                    for child in publication.operational(conn,row)]
            queries = {
                'choices':('entry_id=? AND field=?',(row['entry_id'],row['field'])),
                'selections':('owner=? AND subject=? AND field=?',(owner,source.get('recording_id',source.get('entry_id')),row['field'])),
                'hints':('entry_id=? AND field=? AND source=?',(row['entry_id'],row['field'],source.get('source'))),
                'identifiers':('legacy_id=?',(source.get('id'),)),
                'native':('legacy_id=?',(source.get('id'),)),
                'fields':('owner=? AND source_key=?',(owner,key)),
            }
            for name,expected_rows in expected_relations.items():
                # Only this owner can contribute these identities. Counts below
                # reject unrelated rows without confusing another owner's key.
                if not expected_rows:
                    continue
                predicate,parameters = queries[name]
                saved = list(map(tuple,conn.execute('SELECT * FROM '+table(name)+' WHERE stage=? AND '+predicate+
                    (' ORDER BY position' if name=='fields' else ''),(identity,*parameters))))
                if saved!=expected_rows:
                    raise MigrationError('Metadata '+name+' links differ from their supplying evidence')
                expected_counts[name] += len(expected_rows)
        count += len(rows)
        cursor = Stage(identity,current.proof,rows[-1]['owner'],rows[-1]['source_key'],0,0)
    if current.copied!=count or conn.execute('SELECT count(*) FROM '+table('records')+' WHERE stage=?',(identity,)).fetchone()[0]!=count:
        raise MigrationError('Metadata destination has missing or extra records')
    for name,expected in expected_counts.items():
        if conn.execute('SELECT count(*) FROM '+table(name)+' WHERE stage=?',(identity,)).fetchone()[0]!=expected:
            raise MigrationError('Metadata destination has missing or extra '+name)
    after = ''
    while sources := conn.execute('SELECT source_key,substr(CAST(payload AS BLOB),1,?) AS payload FROM rec_musical_sources '
            'WHERE source_key>? ORDER BY source_key LIMIT 16',(256*1024+1,after)).fetchall():
        for source in sources:
            if len(source['payload'])>256*1024:
                raise WorkBudgetExceeded('Source profile input exceeds byte budget')
            if source_values(conn,source['source_key'],identity=identity)!=json.loads(source['payload']):
                raise MigrationError('Metadata source projection differs from its retained evidence: '+source['source_key'])
        after = sources[-1]['source_key']
    if captured(conn,identity)!=current.proof:
        raise MigrationError('Metadata source changed during destination verification')
    return current


def publish(conn, *, identity, clock=time.time):
    install(conn)
    try:
        current = verify(conn,identity)
    except Exception as exc:
        if conn.execute('SELECT 1 FROM '+publication.table('stages')+' WHERE id=?',(identity,)).fetchone():
            record_error(conn,identity,exc,replace=False)
        raise
    try:
        conn.execute('BEGIN IMMEDIATE')
        if captured(conn,identity)!=current.proof or stage(conn,identity)!=current:
            raise MigrationError('Metadata inputs changed after destination verification')
        conn.execute('INSERT INTO '+table('head')+' VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,published_at=excluded.published_at',
            (identity,clock()))
        conn.execute('INSERT INTO '+publication.table('head')+' VALUES (1,?,1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1,published_at=excluded.published_at',
            (identity,clock()))
        conn.commit()
    finally:
        conn.rollback()


def record_error(conn, identity, exc, *, replace=True):
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO '+table('failures')+' VALUES (?,?,?) ON CONFLICT(stage) DO '+
            ('UPDATE SET error=excluded.error,failed_at=excluded.failed_at' if replace else 'NOTHING'),
            (identity,str(exc)[:2000],time.time()))
        conn.commit()
    finally:
        conn.rollback()


def selected(conn, *, store=None):
    with ExitStack() as guards:
        if store is not None:
            for owner in (publication.table('head'), table('head')):
                guards.enter_context(keyed_reads(store, owner, [1]))
        revision = publication.selected(conn)
        if revision is None or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+table('head')+' WHERE id=1').fetchone()
        return revision if row and row[0]==revision else None


def source_references(conn, key, identity, *, store=None):
    """Bound the last supplying observation for each supported source field."""
    with ExitStack() as guards:
        if store is not None:
            guards.enter_context(keyed_reads(store, table('native'), [key]))
        return tuple(tuple(row) for field in sorted(metadata_native.FIELDS) if (row := conn.execute(
            'SELECT legacy_id,delivery_id FROM '+table('native')+' INDEXED BY '+table('native_source')+
            ' WHERE stage=? AND source_key=? AND field=? ORDER BY legacy_id DESC LIMIT 1',
            (identity,key,field)).fetchone()))


def original(delivery):
    return files.read(delivery['filename'],delivery['digest'],delivery['byte_size'],delivery['byte_offset'])['row']


def operational_fields(conn, owner, key):
    revision = selected(conn)
    if revision is None:
        return None
    row = conn.execute('SELECT d.* FROM '+table('records')+' r JOIN '+table('deliveries')+
        ' d ON d.id=r.delivery_id WHERE r.stage=? AND r.owner=? AND r.source_key=?',(revision,owner,key)).fetchone()
    if row is None:
        return None
    values = json.loads(original(row)['fields_json'])
    fields = conn.execute('SELECT field FROM '+table('fields')+' WHERE stage=? AND owner=? AND source_key=? ORDER BY position',
        (revision,owner,key)).fetchall()
    if not isinstance(values,dict) or list(values)!=[row[0] for row in fields]:
        raise MigrationError('Metadata operational fields differ from their retained delivery')
    return values


def native_rows(conn, *, identity=None, source=None, recording=None, after=0, limit=100, max_bytes=8*1024*1024,
                store=None):
    identity = selected(conn,store=store) if identity is None else identity
    if identity is None:
        return None
    if source is not None:
        # The producer's saved source identity and the last finding for each
        # field establish these dependencies. Full cache equality is verified
        # before selection; no equal-value join establishes attribution.
        with ExitStack() as guards:
            if store is not None:
                references = source_references(conn,source,identity,store=store)
                guards.enter_context(keyed_reads(store,table('native'),[source]))
                guards.enter_context(keyed_reads(store,table('deliveries'),[row[1] for row in references]))
            rows = [row for field in sorted(metadata_native.FIELDS) if (row := conn.execute(
                'SELECT d.*,n.legacy_id FROM '+table('native')+' n INDEXED BY '+table('native_source')+' JOIN '+table('deliveries')+
                ' d ON d.id=n.delivery_id WHERE n.stage=? AND n.source_key=? AND n.field=? ORDER BY n.legacy_id DESC LIMIT 1',
                (identity,source,field)).fetchone())]
    else:
        rows = conn.execute('SELECT d.*,n.legacy_id FROM '+table('native')+' n JOIN '+table('deliveries')+
            ' d ON d.id=n.delivery_id WHERE n.stage=? AND n.recording_id=? AND n.legacy_id>? ORDER BY n.legacy_id LIMIT ?',
            (identity,recording,after,limit)).fetchall()
    if sum(row['byte_size'] for row in rows)>max_bytes:
        raise WorkBudgetExceeded('Native metadata page exceeds byte budget')
    return [original(row) for row in rows]


def source_values(conn, key, max_bytes=256*1024, *, identity=None, store=None):
    rows = native_rows(conn,identity=identity,source=key,max_bytes=max_bytes,store=store)
    if rows is None:
        return None
    return {row['field']:metadata_native._profile_value({**json.loads(row['provenance_json']),
        'field':row['field'],'value':json.loads(row['value_json'])}) for row in rows}


def repair(conn, delivery_id):
    """Rebuild one missing bounded pack from retained, immutable originals."""
    if conn.in_transaction:
        raise MigrationError('Metadata file recovery requires an idle connection')
    target = conn.execute('SELECT filename FROM '+table('deliveries')+' WHERE id=?',(delivery_id,)).fetchone()
    if target is None:
        raise MigrationError('Unknown metadata delivery')
    path = Path(target[0])
    rows = conn.execute('SELECT d.*,o.source_snapshot FROM '+table('deliveries')+' d JOIN '+preparation.ORIGINALS+
        ' o ON o.owner=d.owner AND o.source_key=d.source_key AND o.digest=d.source_digest WHERE d.filename=? ORDER BY d.byte_offset LIMIT 17',
        (str(path),)).fetchall()
    if not 1<=len(rows)<=16:
        raise MigrationError('Metadata recovery pack exceeds its bounded row budget')
    packed = bytearray(files.PACK_HEADER)
    for row in rows:
        encoded = files.encode(document(row))
        if (len(packed)!=row['byte_offset'] or len(encoded)!=row['byte_size']
                or hashlib.sha256(encoded).hexdigest()!=row['digest']
                or hashlib.sha256(row['source_snapshot'].encode()).hexdigest()!=row['source_digest']):
            raise MigrationError('Metadata recovery original differs from its retained range')
        packed.extend(encoded)
    if hashlib.sha256(packed).hexdigest()!=path.stem:
        raise MigrationError('Metadata recovery pack differs from its retained filename')
    from ..policy_documents import _publish_encoded
    rebuilt = _publish_encoded(directory(conn),bytes(packed),path.stem)
    if str(rebuilt)!=str(path):
        raise MigrationError('Metadata recovery changed its retained path')


def explain(conn, observation_id):
    row = conn.execute('SELECT * FROM '+table('observations')+' WHERE id=?',(observation_id,)).fetchone()
    if row is None:
        raise MigrationError('Metadata observation is missing')
    delivery = conn.execute('SELECT * FROM '+table('deliveries')+' WHERE id=?',(row['delivery_id'],)).fetchone()
    return {'observation':dict(row),'delivery':dict(delivery),'derived_from':None}


def lineage(conn, entry, recording):
    revision = selected(conn)
    if revision is None:
        return None
    selections = {row['field']:dict(row) for row in conn.execute('SELECT * FROM '+table('selections')+
        ' WHERE stage=? AND ((owner=\'recording_field_sources\' AND subject=?) OR (owner=\'item_field_sources\' AND subject=?))',
        (revision,recording,entry))}
    choices = {row['field']:explain(conn,row['observation_id']) for row in conn.execute('SELECT * FROM '+table('choices')+
        ' WHERE stage=? AND entry_id=?',(revision,entry))}
    return {field:{'selection':selections.get(field),'evidence':None,'choice':choices.get(field)}
        for field in metadata_legacy.CATALOG_FIELDS}


def main(argv=None):
    import argparse
    from contextlib import closing
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--stage',type=int,required=True)
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--retry',action='store_true')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--repair-delivery',type=int)
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=16)
    args = parser.parse_args(argv)
    if not 1<=args.batches<=128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+
                ('?mode=ro' if args.status else '?mode=rw'),uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.repair_delivery is not None:
                repair(conn,args.repair_delivery)
                print(json.dumps({'repaired':args.repair_delivery}))
            elif args.status:
                exists = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone()
                current = stage(conn,args.stage) if exists else None
                failure = conn.execute('SELECT error FROM '+table('failures')+' WHERE stage=?',(args.stage,)).fetchone() if exists else None
                print(json.dumps({'stage':asdict(current) if current else None,'error':failure[0] if failure else None}))
            elif args.publish:
                publish(conn,identity=args.stage)
                print(json.dumps({'published':args.stage}))
            else:
                for _ in range(args.batches):
                    current = step(conn,identity=args.stage,batch_size=args.batch_size,retry=args.retry)
                    print(json.dumps(asdict(current)))
                    if current.complete:
                        break
    except (ValueError,OSError,sqlite3.Error,MigrationError) as exc:
        parser.exit(1,str(exc)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
