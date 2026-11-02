"""Select verified learning relations after bounded retention of exact group aliases."""
from dataclasses import asdict, replace
import hashlib
import json
import time
from types import SimpleNamespace

from .. import choice_learning_state as learning, group_evidence
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import choice_learning as preparation
from .mapped_publication import MappedPublication


CURRENT = 'prepared_choice_learning_publication_heads'
ROOTS = 'prepared_choice_learning_publication_roots'
ROOT_PROGRESS = 'prepared_choice_learning_root_progress'


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='choice_learning_heads' AND type='table'").fetchone() is not None


def _source(conn, surface):
    return conn.execute('SELECT '+','.join(preparation.DEFINITION.projections['rec_proposals'])+
        ' FROM main.rec_proposals WHERE surface=?', (surface,)).fetchone()


def _expected(conn, surface, value):
    """Describe every relational column, including masked and unresolved fields."""
    for field, owner in (('parameters','choice_learning_parameters'),('excluded','choice_learning_exclusions')):
        for key, number in value.get(field,{}).items():
            yield owner, (key,number)
    if surface in ('choice_bandit','group_choice_bandit'):
        fields = learning.GROUP if surface=='group_choice_bandit' else learning.MODE
        scalar = fields[:-1] if surface=='group_choice_bandit' else fields
        for position, row in enumerate(value.get('rows',[])):
            data = (position,learning._mask(row,fields),*(row.get(field) for field in scalar))
            if surface=='choice_bandit':
                found = conn.execute('SELECT mode_id FROM taste_modes WHERE mode_id=?',(row.get('mode_id'),)).fetchone()
                yield 'choice_mode_estimates', (*data,found[0] if found else None,None if found else 'missing_mode')
            else:
                yield 'choice_group_estimates', data
                for offset, menu in enumerate(row.get('menus',[])):
                    yield 'choice_group_opportunities', (position,offset,menu)
    for position, row in enumerate(value.get('contexts',[])):
        album = learning._album(conn,row.get('option_id')) if row.get('format')=='albums' else None
        yield 'choice_interest_contexts', (position,learning._mask(row,learning.CONTEXT),
            *(row.get(field) for field in learning.CONTEXT[:-2]),int(row.get('feature') is None),
            album,'missing_album' if row.get('format')=='albums' and album is None else None)
        for dimension, number in enumerate(row.get('feature') or []):
            yield 'choice_interest_features', (position,dimension,number)
        for offset, key in enumerate(row.get('keys',[])):
            recording = learning._recording(conn,key)
            yield 'choice_interest_tracks', (position,offset,key,recording,None if recording else 'missing_recording')
    for position, (browse, tracks) in enumerate(value.get('album_track_days',{}).items()):
        album = learning._album(conn,browse)
        yield 'choice_album_interests', (position,browse,album,None if album else 'missing_album')
        for offset, (key, days) in enumerate(tracks.items()):
            recording = learning._recording(conn,key)
            yield 'choice_album_track_days', (position,offset,key,recording,days,None if recording else 'missing_recording')


def _references(value, parts):
    position = parts+len(value.get('parameters',{}))+len(value.get('excluded',{}))
    if value.get('generation') is not None:
        yield position,'generation',value['generation'],None,None
        position += 1
    for index,row in enumerate(value.get('rows',[])):
        position += 1+len(row.get('menus',[]))
        for field in ('version','shared_from'):
            if row.get(field):
                yield position,field,row[field],index,row.get('group_id') if field=='version' else None
                position += 1


def _resolved(conn, field, raw, group, generation):
    if field=='generation':
        row = conn.execute('SELECT g.id FROM group_generations g JOIN group_publications p ON p.generation_id=g.id '
            'WHERE g.external_key=?',(raw,)).fetchone()
        return (row[0],None,None) if row else (None,None,'missing_generation')
    row = conn.execute('SELECT a.group_version_id,g.external_key FROM group_version_aliases a '
        'JOIN group_versions v ON v.id=a.group_version_id JOIN taste_groups g ON g.id=v.group_id '
        'JOIN group_publications p ON p.generation_id=a.first_generation_id WHERE a.external_version=?',(raw,)).fetchone()
    if row is None:
        return None,None,'missing_version'
    if group is not None and row[1]!=group:
        return None,None,'wrong_group'
    if group is not None and generation is not None and not conn.execute(
        'SELECT 1 FROM generation_groups x JOIN group_generations g ON g.id=x.generation_id '
        'JOIN group_publications p ON p.generation_id=g.id WHERE g.external_key=? AND x.external_version=? '
        'AND x.group_version_id=?',(generation,raw,row[0])).fetchone():
        return None,None,'wrong_generation'
    return None,row[0],None


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Learning verification requires an idle connection')
    source = _source(conn,header['surface'])
    if source is None:
        raise MigrationError('Learning source is missing')
    raw = bytes(source['payload']).decode()
    if preparation.read_original(conn,header)!=raw:
        raise MigrationError('Learning original differs from its source')
    value = learning.decode(header['surface'],raw)
    expected = (header['id'],header['surface'],learning._mask(value,learning.HEADER),source['built_at'],
        *(value.get(field) for field in learning.SCALARS),header['input_revision'],
        hashlib.sha256(raw.encode()).hexdigest(),len(raw.encode()),header['part_count'])
    if tuple(header)!=expected:
        raise MigrationError('Learning header differs from its source')
    try:
        conn.execute('BEGIN')
        if learning._read(conn,header['surface'],header['id'],prefix='prepared_')!=value:
            raise MigrationError('Learning serving view differs from its source')
        readers = {owner:iter(conn.execute('SELECT * FROM prepared_'+owner+' WHERE publication_id=?',
            (header['id'],))) for owner in preparation.TABLES[1:]}
        # Relations with named keys must sort independently of document key order.
        expected_rows = {owner:[] for owner in readers}
        for owner,row in _expected(conn,header['surface'],value):
            expected_rows[owner].append(row)
        for owner, reader in readers.items():
            actual = [tuple(row)[1:] for row in reader]
            if sorted(actual,key=canonical)!=sorted(expected_rows[owner],key=canonical):
                raise MigrationError('Learning relation differs from its source or references: '+owner)
        refs = list(conn.execute('SELECT position,field,raw_key,estimate_position,expected_group,generation_id,group_version_id,unresolved_reason '
            'FROM '+preparation.REFERENCES+' WHERE publication_id=? ORDER BY position',(header['id'],)))
        expected_refs = [(*reference,*_resolved(conn,reference[1],reference[2],reference[4],value.get('generation')))
            for reference in _references(value,header['part_count'])] if header['surface']=='group_choice_bandit' else []
        if list(map(tuple,refs))!=expected_refs:
            raise MigrationError('Learning group references differ from their source')
    finally:
        conn.rollback()


class ChoicePublication(MappedPublication):
    def definitions(self):
        # Child relations use publication_id rather than the header's compound
        # key. Supply their explicit custody programs after the common map DDL.
        base = replace(self,worker=SimpleNamespace(definition=replace(preparation.DEFINITION,destinations=())))
        yield CURRENT, (f'CREATE VIEW {CURRENT} AS SELECT h.surface,p.input_revision FROM {preparation.CURRENT} h '
            f'JOIN {preparation.PUBLICATIONS} p ON p.id=h.publication_id')
        yield from MappedPublication.definitions(base)
        yield ROOT_PROGRESS, f'''CREATE TABLE {ROOT_PROGRESS} (
            stage INTEGER PRIMARY KEY REFERENCES {self.table('stages')}(id),publication_id INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT -1,complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
        yield ROOTS, f'''CREATE TABLE {ROOTS} (
            publication_id INTEGER NOT NULL REFERENCES {preparation.PUBLICATIONS}(id),position INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind='prepared'),external_key TEXT NOT NULL UNIQUE,
            PRIMARY KEY(publication_id,position),
            FOREIGN KEY(publication_id,position) REFERENCES {preparation.REFERENCES}(publication_id,position),
            FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)) WITHOUT ROWID'''
        owners = [name for name,sql in preparation.DEFINITION.destinations if sql.startswith('CREATE TABLE')]
        for owner in owners:
            for operation in ('INSERT','UPDATE','DELETE'):
                sides = ('OLD','NEW') if operation=='UPDATE' else ('OLD',) if operation=='DELETE' else ('NEW',)
                if owner!=preparation.CURRENT:
                    conditions = []
                    for side in sides:
                        condition = (f'c.source={side}.surface AND c.generation={side}.input_revision' if owner==preparation.PUBLICATIONS else
                            f'c.source=p.surface AND c.generation=p.input_revision AND p.id={side}.publication_id')
                        conditions.append('EXISTS(SELECT 1 FROM '+self.table('custody')+' c'+
                            ('' if owner==preparation.PUBLICATIONS else ' JOIN '+preparation.PUBLICATIONS+' p ON 1=1')+' WHERE '+condition+')')
                    name = owner+'_learning_custody_'+operation.lower()
                    yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} WHEN '+ ' OR '.join(conditions)+
                        " BEGIN SELECT RAISE(ABORT,'Published learning custody is retained'); END")
                name = owner+'_learning_epoch_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN '
                    f'UPDATE {self.table("epoch")} SET revision=revision+1 WHERE id=1; END')
        for owner in (ROOTS,ROOT_PROGRESS):
            for operation in ('UPDATE','DELETE'):
                name = owner+'_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '+
                    ('WHEN OLD.complete=1 ' if owner==ROOT_PROGRESS else '')+
                    "BEGIN SELECT RAISE(ABORT,'Learning publication roots are retained'); END")
        for owner in ('group_evidence_owners','group_evidence_generations','group_evidence_versions',
                      'unresolved_group_evidence','unresolved_group_contexts'):
            for operation in ('INSERT','UPDATE','DELETE'):
                sides = ('OLD','NEW') if operation=='UPDATE' else ('OLD',) if operation=='DELETE' else ('NEW',)
                name = ROOTS+'_'+owner+'_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} WHEN '+
                    ' OR '.join(f'EXISTS(SELECT 1 FROM {ROOTS} WHERE kind={side}.kind AND external_key={side}.external_key)' for side in sides)+
                    " BEGIN SELECT RAISE(ABORT,'Published learning group evidence is retained'); END")

    def verify(self,conn,identity):
        result = super().verify(conn,identity)
        progress = conn.execute('SELECT complete FROM '+ROOT_PROGRESS+' WHERE stage=?',(identity,)).fetchone()
        if progress is None or progress[0]!=1:
            raise MigrationError('Learning group custody is incomplete')
        refs = conn.execute('SELECT r.*,p.generation FROM '+self.table('map')+' m JOIN '+preparation.PUBLICATIONS+
            ' p ON p.surface=m.source AND p.input_revision=m.generation JOIN '+preparation.REFERENCES+
            ' r ON r.publication_id=p.id WHERE m.stage=? ORDER BY r.publication_id,r.position',(identity,))
        count = 0
        for ref in refs:
            root = conn.execute('SELECT kind,external_key FROM '+ROOTS+' WHERE publication_id=? AND position=?',
                (ref['publication_id'],ref['position'])).fetchone()
            if root is None:
                raise MigrationError('Learning reference has no retention owner')
            context = reference_context(ref)
            generation,versions = group_evidence.references(context)
            digest = hashlib.sha256(canonical([generation,versions])).hexdigest()
            owner = conn.execute('SELECT reference_digest FROM group_evidence_owners WHERE kind=? AND external_key=?',tuple(root)).fetchone()
            if owner is None or owner[0]!=digest:
                raise MigrationError('Learning retention owner differs from its exact reference')
            actual_generation = [row[0] for row in conn.execute('SELECT generation_id FROM group_evidence_generations WHERE kind=? AND external_key=?',tuple(root))]
            actual_versions = list(map(tuple,conn.execute('SELECT external_version,group_version_id FROM group_evidence_versions '
                'WHERE kind=? AND external_key=? ORDER BY external_version',tuple(root))))
            unresolved = list(map(tuple,conn.execute('SELECT reference_kind,raw_key,reason FROM unresolved_group_evidence '
                'WHERE kind=? AND external_key=? ORDER BY reference_kind,raw_key',tuple(root))))
            expected_generation = _resolved(conn,'generation',generation,None,None) if generation is not None else (None,None,None)
            expected_versions,expected_unresolved = [],[]
            if expected_generation[2]:
                expected_unresolved.append(('generation',generation,expected_generation[2]))
            for raw,group in versions:
                _,version,reason = _resolved(conn,'version',raw,group,generation)
                if reason:
                    expected_unresolved.append(('version',raw,reason))
                else:
                    expected_versions.append((raw,version))
            if (actual_generation!=([expected_generation[0]] if expected_generation[0] is not None else []) or
                    actual_versions!=expected_versions or unresolved!=sorted(expected_unresolved)):
                raise MigrationError('Learning retention roots differ from current exact references')
            count += 1
        # ROOTS may also retain prior stages; only references of this map count.
        if conn.execute('SELECT count(*) FROM '+ROOTS+' r JOIN '+preparation.PUBLICATIONS+
            ' p ON p.id=r.publication_id JOIN '+self.table('map')+
            ' m ON m.source=p.surface AND m.generation=p.input_revision WHERE m.stage=?',(identity,)).fetchone()[0]!=count:
            raise MigrationError('Learning publication has extra retention owners')
        return result


OWNER = ChoicePublication('choice-learning',preparation.JOB,preparation.PUBLICATIONS,CURRENT,native,
    verify_document,preparation.read_original,source_column='surface',generation_column='input_revision')


def reference_context(ref):
    context = {'group_generation':ref['raw_key'] if ref['field']=='generation' else ref['generation']}
    if ref['field']!='generation':
        context['musical_groups'] = [{'version':ref['raw_key'],**({'group_id':ref['expected_group']} if ref['expected_group'] is not None else {})}]
    return context


def step(conn,*,batch_size=16,clock=time.time):
    if type(batch_size) is not int or not 1<=batch_size<=16:
        raise ValueError('Learning publication requires 1 to 16 references per batch')
    mapped = OWNER.step(conn,batch_size=batch_size)
    if not mapped.complete:
        return {**asdict(mapped),'complete':0}
    try:
        conn.execute('BEGIN IMMEDIATE')
        progress,epoch = OWNER.proof(conn)
        if (progress.input_revision,epoch)!=(mapped.input_revision,mapped.custody_revision):
            raise MigrationError('Learning changed before group retention; reconcile again')
        current = conn.execute('SELECT * FROM '+ROOT_PROGRESS+' WHERE stage=?',(mapped.id,)).fetchone()
        if current is None:
            conn.execute('INSERT INTO '+ROOT_PROGRESS+'(stage,complete) VALUES (?,0)',(mapped.id,))
            current = conn.execute('SELECT * FROM '+ROOT_PROGRESS+' WHERE stage=?',(mapped.id,)).fetchone()
        if not current['complete']:
            refs = conn.execute('SELECT r.*,p.generation FROM '+OWNER.table('map')+' m JOIN '+preparation.PUBLICATIONS+
                ' p ON p.surface=m.source AND p.input_revision=m.generation JOIN '+preparation.REFERENCES+
                ' r ON r.publication_id=p.id WHERE m.stage=? AND (r.publication_id,r.position)>(?,?) '
                'ORDER BY r.publication_id,r.position LIMIT ?',
                (mapped.id,current['publication_id'],current['position'],batch_size+1)).fetchall()
            for ref in refs[:batch_size]:
                key = f'choice-preparation:{ref["publication_id"]}:{ref["position"]}'
                group_evidence.retain(conn,'prepared',key,reference_context(ref),clock(),strict=False)
                conn.execute('INSERT INTO '+ROOTS+' VALUES (?,?,\'prepared\',?) ON CONFLICT DO NOTHING',
                    (ref['publication_id'],ref['position'],key))
            last = refs[min(len(refs),batch_size)-1] if refs else current
            conn.execute('UPDATE '+ROOT_PROGRESS+' SET publication_id=?,position=?,complete=? WHERE stage=?',
                (last['publication_id'],last['position'],int(len(refs)<=batch_size),mapped.id))
        complete = conn.execute('SELECT complete FROM '+ROOT_PROGRESS+' WHERE stage=?',(mapped.id,)).fetchone()[0]
        conn.commit()
        return {**asdict(mapped),'complete':complete}
    finally:
        conn.rollback()


def selected(conn):
    return OWNER.selected(conn)


def read(conn,surface):
    identity = selected(conn)
    if identity is None:
        return None
    header = OWNER.capture(conn,identity,surface)
    return learning._read(conn,surface,header['id'],prefix='prepared_') if header else None


def validate_installed(conn):
    OWNER.validate_installed(conn)


def repair(conn,surface):
    if conn.in_transaction:
        raise MigrationError('Learning file recovery requires an idle connection')
    source = _source(conn,surface)
    header = conn.execute('SELECT p.* FROM '+preparation.CURRENT+' h JOIN '+preparation.PUBLICATIONS+
        ' p ON p.id=h.publication_id WHERE h.surface=?',(surface,)).fetchone()
    if source is None or header is None or hashlib.sha256(bytes(source['payload'])).hexdigest()!=header['raw_digest']:
        raise MigrationError('Learning source changed or is missing; reconcile before file recovery')
    plan = preparation.prepare(conn,source)
    if (plan.raw_digest,plan.raw_size,plan.part_count)!=(header['raw_digest'],header['raw_size'],header['part_count']):
        raise MigrationError('Rebuilt learning original differs from its captured source')


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage',type=int)
    action.add_argument('--repair-source',choices=sorted(learning.SURFACES))
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=16)
    args = parser.parse_args(argv)
    if not 1<=args.batches<=128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw',uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                OWNER.publish(conn,stage=args.publish_stage)
                result = {'selected':selected(conn)}
            elif args.repair_source is not None:
                repair(conn,args.repair_source)
                result = {'repaired':args.repair_source}
            else:
                for _ in range(args.batches):
                    result = step(conn,batch_size=args.batch_size)
                    if result['complete']:
                        break
            print(json.dumps(result))
    except (MigrationError,sqlite3.Error,ValueError,OSError) as exc:
        parser.exit(1,f'Choice learning publication failed: {exc}\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
