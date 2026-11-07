"""Canonical group content shared by independently retained publications."""
import json
import math

from yt_playlist.core import group_content, group_documents, group_storage, policy_documents
from yt_playlist.core.evidence_ledger import canonical
from .runner import MigrationError

OLD_TABLES = ('rec_group_assignments', 'rec_group_graph', 'rec_group_unknown', 'rec_group_lineage',
              'rec_group_members', 'rec_group_head', 'rec_group_builds', 'rec_group_publications',
              'rec_group_definitions', 'rec_group_identities', 'rec_group_generations')
GENERATION_BYTES = 64 * 1024 * 1024


def _finite(name, *, nullable=False):
    condition = f"typeof({name}) IN ('real','integer') AND abs({name})<=1.7976931348623157e308"
    return f'CHECK({name} IS NULL OR ({condition}))' if nullable else f'CHECK({condition})'


def _digest(name):
    return f"CHECK(length({name})=64 AND {name} NOT GLOB '*[^0-9a-f]*')"


def _file(prefix):
    return (f'{prefix}_filename TEXT NOT NULL CHECK(length({prefix}_filename)>0),'
            f'{prefix}_digest TEXT NOT NULL {_digest(prefix + "_digest")},'
            f'{prefix}_bytes INTEGER NOT NULL CHECK(typeof({prefix}_bytes)=\'integer\' '
            f'AND {prefix}_bytes BETWEEN 1 AND 8388608),'
            f'{prefix}_offset INTEGER NOT NULL CHECK(typeof({prefix}_offset)=\'integer\' AND {prefix}_offset>=29)')


def statements():
    yield ('CREATE TABLE taste_groups (id INTEGER PRIMARY KEY AUTOINCREMENT,external_key TEXT NOT NULL UNIQUE '
           'CHECK(length(external_key)>0),legacy_mode_id INTEGER UNIQUE,created_at REAL NOT NULL '
           + _finite('created_at') + ')')
    yield ('CREATE TABLE group_input_manifests (id INTEGER PRIMARY KEY AUTOINCREMENT,content_digest TEXT NOT NULL '
           'UNIQUE ' + _digest('content_digest') + ",complete INTEGER NOT NULL DEFAULT 0 "
           "CHECK(typeof(complete)='integer' AND complete IN (0,1)))")
    yield ('CREATE TABLE group_recording_inputs (id INTEGER PRIMARY KEY AUTOINCREMENT,recording_id INTEGER NOT NULL '
           'REFERENCES recordings(id),input_digest TEXT NOT NULL ' + _digest('input_digest')
           + ',UNIQUE(recording_id,input_digest),UNIQUE(id,recording_id))')
    yield ('CREATE TABLE group_manifest_inputs (manifest_id INTEGER NOT NULL REFERENCES group_input_manifests(id),'
           'recording_id INTEGER NOT NULL REFERENCES recordings(id),input_id INTEGER NOT NULL,'
           'PRIMARY KEY(manifest_id,recording_id),FOREIGN KEY(input_id,recording_id) '
           'REFERENCES group_recording_inputs(id,recording_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_manifest_input ON group_manifest_inputs(input_id,manifest_id)'
    yield ('CREATE TABLE unresolved_group_manifest_inputs (manifest_id INTEGER NOT NULL REFERENCES '
           'group_input_manifests(id),raw_key TEXT NOT NULL,input_digest TEXT NOT NULL ' + _digest('input_digest')
           + ",reason TEXT NOT NULL CHECK(reason='missing_recording'),PRIMARY KEY(manifest_id,raw_key)) WITHOUT ROWID")
    yield ('CREATE TABLE group_generations (id INTEGER PRIMARY KEY AUTOINCREMENT,external_key TEXT NOT NULL UNIQUE '
           'CHECK(length(external_key)>0),policy_version TEXT NOT NULL REFERENCES recommendation_policies(version),'
           'input_fingerprint TEXT NOT NULL,input_manifest_id INTEGER NOT NULL REFERENCES group_input_manifests(id),'
           "input_serial INTEGER NOT NULL CHECK(typeof(input_serial)='integer' AND input_serial>=0),"
           'created_at REAL NOT NULL ' + _finite('created_at') + ',' + _file('metadata') + ')')
    yield ('CREATE TABLE group_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,group_id INTEGER NOT NULL '
           'REFERENCES taste_groups(id),parent_group_id INTEGER REFERENCES taste_groups(id),content_digest TEXT '
           'NOT NULL ' + _digest('content_digest') + ',policy_version TEXT NOT NULL REFERENCES '
           'recommendation_policies(version),'
           'created_at REAL NOT NULL ' + _finite('created_at') + ',' + _file('definition') + ','
           "leaf INTEGER NOT NULL CHECK(typeof(leaf)='integer' AND leaf IN (0,1)),"
           "legacy INTEGER NOT NULL CHECK(typeof(legacy)='integer' AND legacy IN (0,1)),"
           "membership_known INTEGER NOT NULL CHECK(typeof(membership_known)='integer' AND membership_known IN (0,1)),"
           "depth INTEGER CHECK(depth IS NULL OR (typeof(depth)='integer' AND depth>=0)),"
           "size INTEGER NOT NULL CHECK(typeof(size)='integer' AND size>=0),"
           'CHECK(legacy=0 OR membership_known=0),'
           'CHECK(parent_group_id IS NULL OR parent_group_id<>group_id),'
           'UNIQUE(group_id,content_digest),UNIQUE(id,group_id))')
    yield 'CREATE INDEX ix_group_version_parent ON group_versions(parent_group_id,id)'
    yield 'CREATE INDEX ix_group_generation_manifest ON group_generations(input_manifest_id,id)'
    yield 'CREATE INDEX ix_group_generation_policy ON group_generations(policy_version,id)'
    yield 'CREATE INDEX ix_group_version_policy ON group_versions(policy_version,id)'
    yield ('CREATE TABLE group_version_aliases (external_version TEXT PRIMARY KEY NOT NULL '
           'CHECK(length(external_version)>0),group_version_id INTEGER NOT NULL REFERENCES group_versions(id),'
           'first_generation_id INTEGER NOT NULL REFERENCES group_generations(id),'
           'UNIQUE(external_version,group_version_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_version_alias_owner ON group_version_aliases(group_version_id,external_version)'
    yield ('CREATE TABLE generation_groups (generation_id INTEGER NOT NULL REFERENCES group_generations(id),'
           'group_id INTEGER NOT NULL REFERENCES taste_groups(id),group_version_id INTEGER NOT NULL,'
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),"
           'external_version TEXT NOT NULL,PRIMARY KEY(generation_id,group_id),UNIQUE(generation_id,position),'
           'UNIQUE(generation_id,external_version),UNIQUE(generation_id,group_version_id),FOREIGN KEY(group_version_id,group_id) '
           'REFERENCES group_versions(id,group_id),FOREIGN KEY(external_version,group_version_id) '
           'REFERENCES group_version_aliases(external_version,group_version_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_generation_group_version ON generation_groups(group_version_id,generation_id)'
    yield 'CREATE INDEX ix_generation_group_alias ON generation_groups(external_version,generation_id)'
    yield ('CREATE TABLE group_members (group_version_id INTEGER NOT NULL REFERENCES group_versions(id),'
           'recording_id INTEGER NOT NULL REFERENCES recordings(id),similarity REAL NOT NULL '
           'CHECK(similarity BETWEEN 0 AND 1),support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),'
           'input_id INTEGER,PRIMARY KEY(group_version_id,recording_id),'
           'FOREIGN KEY(input_id,recording_id) REFERENCES group_recording_inputs(id,recording_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_member_recording ON group_members(recording_id,group_version_id)'
    yield 'CREATE INDEX ix_group_member_input ON group_members(input_id) WHERE input_id IS NOT NULL'
    yield ('CREATE TABLE unresolved_group_members (group_version_id INTEGER NOT NULL REFERENCES group_versions(id),'
           'raw_key TEXT NOT NULL,similarity REAL NOT NULL CHECK(similarity BETWEEN 0 AND 1),support REAL NOT NULL '
           "CHECK(support BETWEEN 0 AND 1),reason TEXT NOT NULL CHECK(reason='missing_recording'),"
           'input_digest TEXT CHECK(input_digest IS NULL OR (length(input_digest)=64 '
           "AND input_digest NOT GLOB '*[^0-9a-f]*')),PRIMARY KEY(group_version_id,raw_key)) WITHOUT ROWID")
    yield ('CREATE TABLE group_representatives (group_version_id INTEGER NOT NULL REFERENCES group_versions(id),'
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),"
           'recording_id INTEGER REFERENCES recordings(id),raw_key TEXT,PRIMARY KEY(group_version_id,position),'
           'CHECK((recording_id IS NOT NULL AND raw_key IS NULL) OR '
           '(recording_id IS NULL AND raw_key IS NOT NULL))) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_representative_recording ON group_representatives(recording_id,group_version_id)'
    yield ('CREATE TABLE group_publications (generation_id INTEGER PRIMARY KEY NOT NULL '
           'REFERENCES group_generations(id),published_at REAL ' + _finite('published_at', nullable=True)
           + ') WITHOUT ROWID')
    yield ('CREATE TABLE group_head (id INTEGER PRIMARY KEY CHECK(id=1),generation_id INTEGER NOT NULL '
           'REFERENCES group_publications(generation_id))')
    yield ('CREATE TABLE group_builds (generation_id INTEGER PRIMARY KEY NOT NULL REFERENCES group_generations(id),'
           'owner TEXT NOT NULL CHECK(length(owner)>0),expires_at REAL NOT NULL ' + _finite('expires_at')
           + ",ready INTEGER NOT NULL DEFAULT 0 CHECK(typeof(ready)='integer' AND ready IN (0,1)),"
           "output_digest TEXT CHECK(output_digest IS NULL OR (length(output_digest)=64 AND output_digest NOT GLOB '*[^0-9a-f]*')),"
           'parent_generation_id INTEGER REFERENCES group_publications(generation_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_compact_group_build_expiry ON group_builds(expires_at,generation_id)'
    yield 'CREATE INDEX ix_compact_group_build_parent ON group_builds(parent_generation_id,generation_id)'
    yield ('CREATE TABLE group_build_versions (generation_id INTEGER NOT NULL REFERENCES group_builds(generation_id),'
           'group_version_id INTEGER NOT NULL UNIQUE REFERENCES group_versions(id),'
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),"
           'external_version TEXT NOT NULL CHECK(length(external_version)>0),'
           'PRIMARY KEY(generation_id,position),UNIQUE(generation_id,external_version)) WITHOUT ROWID')
    yield ('CREATE TABLE group_lineage (source_alias TEXT NOT NULL,target_alias TEXT NOT NULL,'
           'source_version_id INTEGER NOT NULL,target_version_id INTEGER NOT NULL,relation TEXT NOT NULL '
           "CHECK(relation IN ('continuation','split','merge','legacy')),compatible INTEGER "
           "CHECK(compatible IS NULL OR (typeof(compatible)='integer' AND compatible IN (0,1))),"
           'old_share REAL CHECK(old_share IS NULL OR old_share BETWEEN 0 AND 1),'
           'new_share REAL CHECK(new_share IS NULL OR new_share BETWEEN 0 AND 1),'
           "overlap INTEGER CHECK(overlap IS NULL OR (typeof(overlap)='integer' AND overlap>=0)),"
           + _file('evidence') + ',PRIMARY KEY(source_alias,target_alias),'
           'FOREIGN KEY(source_alias,source_version_id) REFERENCES group_version_aliases(external_version,group_version_id),'
           'FOREIGN KEY(target_alias,target_version_id) REFERENCES group_version_aliases(external_version,group_version_id)) '
           'WITHOUT ROWID')
    yield 'CREATE INDEX ix_compact_group_lineage_source ON group_lineage(source_version_id,target_version_id)'
    yield 'CREATE INDEX ix_compact_group_lineage_target ON group_lineage(target_version_id,source_version_id)'
    yield ('CREATE TABLE group_generation_lineage (generation_id INTEGER NOT NULL,source_alias TEXT NOT NULL,'
           'target_alias TEXT NOT NULL,PRIMARY KEY(generation_id,source_alias,target_alias),'
           'FOREIGN KEY(generation_id,target_alias) REFERENCES generation_groups(generation_id,external_version),'
           'FOREIGN KEY(source_alias,target_alias) REFERENCES group_lineage(source_alias,target_alias)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_generation_lineage_edge ON group_generation_lineage(source_alias,target_alias,generation_id)'
    yield ('CREATE TABLE group_lineage_retirements (source_alias TEXT NOT NULL,target_alias TEXT NOT NULL,'
           'PRIMARY KEY(source_alias,target_alias),FOREIGN KEY(source_alias,target_alias) '
           'REFERENCES group_lineage(source_alias,target_alias)) WITHOUT ROWID')
    yield ('CREATE TABLE group_evidence_owners (kind TEXT NOT NULL CHECK(kind IN '
           "('offer','response','comparison','prepared','pending','checkpoint')),external_key TEXT NOT NULL "
           'CHECK(length(external_key)>0),reference_digest TEXT NOT NULL ' + _digest('reference_digest')
           + ',created_at REAL NOT NULL ' + _finite('created_at') + ',PRIMARY KEY(kind,external_key)) WITHOUT ROWID')
    yield ('CREATE TABLE group_evidence_generations (kind TEXT NOT NULL,external_key TEXT NOT NULL,'
           'generation_id INTEGER NOT NULL REFERENCES group_generations(id),PRIMARY KEY(kind,external_key,generation_id),'
           'FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_evidence_generation ON group_evidence_generations(generation_id,kind,external_key)'
    yield ('CREATE TABLE group_evidence_versions (kind TEXT NOT NULL,external_key TEXT NOT NULL,'
           'external_version TEXT NOT NULL,group_version_id INTEGER NOT NULL,'
           'PRIMARY KEY(kind,external_key,external_version),FOREIGN KEY(kind,external_key) '
           'REFERENCES group_evidence_owners(kind,external_key),FOREIGN KEY(external_version,group_version_id) '
           'REFERENCES group_version_aliases(external_version,group_version_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_evidence_version ON group_evidence_versions(group_version_id,kind,external_key)'
    yield ('CREATE TABLE unresolved_group_evidence (kind TEXT NOT NULL,external_key TEXT NOT NULL,'
           "reference_kind TEXT NOT NULL CHECK(reference_kind IN ('generation','version')),"
           "raw_key TEXT NOT NULL,reason TEXT NOT NULL CHECK(reason IN ('missing_generation','missing_version',"
           "'wrong_generation','wrong_group')),PRIMARY KEY(kind,external_key,reference_kind,raw_key),"
           'FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)) WITHOUT ROWID')
    yield ('CREATE TABLE unresolved_group_contexts (kind TEXT NOT NULL,external_key TEXT NOT NULL,'
           "source_context TEXT NOT NULL,reason TEXT NOT NULL CHECK(reason IN ('missing_context','invalid_context',"
           "'missing_offer','invalid_checkpoint')),PRIMARY KEY(kind,external_key),"
           'FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)) WITHOUT ROWID')
    yield ('CREATE TABLE group_retention_state (id INTEGER PRIMARY KEY CHECK(id=1),roots_complete INTEGER NOT NULL '
           "DEFAULT 0 CHECK(typeof(roots_complete)='integer' AND roots_complete IN (0,1)))")
    yield 'CREATE INDEX ix_group_alias_first_generation ON group_version_aliases(first_generation_id,external_version)'
    yield ('CREATE TABLE group_unknown (generation_id INTEGER NOT NULL REFERENCES group_generations(id),'
           'recording_id INTEGER NOT NULL REFERENCES recordings(id),reason TEXT NOT NULL,'
           'PRIMARY KEY(generation_id,recording_id)) WITHOUT ROWID')
    yield ('CREATE TABLE unresolved_group_unknown (generation_id INTEGER NOT NULL REFERENCES group_generations(id),'
           "raw_key TEXT NOT NULL,reason TEXT NOT NULL,resolution_reason TEXT NOT NULL CHECK(resolution_reason='missing_recording'),"
           'PRIMARY KEY(generation_id,raw_key)) WITHOUT ROWID')
    yield ('CREATE TABLE group_graph (generation_id INTEGER NOT NULL REFERENCES group_generations(id),'
           'left_recording_id INTEGER NOT NULL REFERENCES recordings(id),right_recording_id INTEGER NOT NULL '
           'REFERENCES recordings(id),weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),support REAL NOT NULL '
           'CHECK(support BETWEEN 0 AND 1),PRIMARY KEY(generation_id,left_recording_id,right_recording_id)) WITHOUT ROWID')
    yield ('CREATE TABLE unresolved_group_graph (generation_id INTEGER NOT NULL REFERENCES group_generations(id),'
           'left_key TEXT NOT NULL,right_key TEXT NOT NULL,weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),'
           "support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),reason TEXT NOT NULL CHECK(reason='missing_recording'),"
           'PRIMARY KEY(generation_id,left_key,right_key)) WITHOUT ROWID')
    for name, owner in (('group_assignments', 'recording_id INTEGER NOT NULL REFERENCES recordings(id)'),
                        ('unresolved_group_assignments', 'raw_key TEXT NOT NULL')):
        key = 'recording_id' if name == 'group_assignments' else 'raw_key'
        yield (f'CREATE TABLE {name} (generation_id INTEGER NOT NULL REFERENCES group_generations(id),{owner},'
               'group_version_id INTEGER,similarity REAL NOT NULL CHECK(similarity BETWEEN 0 AND 1),'
               'support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),profile_digest TEXT,prepared_at REAL NOT NULL '
               + _finite('prepared_at') + (",resolution_reason TEXT NOT NULL DEFAULT 'missing_recording' "
                   "CHECK(resolution_reason='missing_recording')" if name == 'unresolved_group_assignments' else '')
               + f',PRIMARY KEY(generation_id,{key}),'
               'FOREIGN KEY(generation_id,group_version_id) REFERENCES generation_groups(generation_id,group_version_id)) '
               'WITHOUT ROWID')
    for table in ('group_versions', 'group_members', 'unresolved_group_members', 'group_input_manifests',
                  'group_recording_inputs', 'group_manifest_inputs', 'unresolved_group_manifest_inputs',
                  'group_version_aliases', 'group_representatives', 'group_lineage'):
        # Files can be relocated by a verified restore; content and ownership
        # cannot change. Deletion is separately fenced by retained references.
        if table in ('group_versions', 'group_lineage'):
            # UPDATE OF avoids treating a filename-only restore as content mutation.
            columns = ('id,group_id,parent_group_id,content_digest,policy_version,created_at,'
                       'definition_digest,definition_bytes,leaf,legacy,membership_known,depth,size'
                       if table == 'group_versions' else
                       'source_alias,target_alias,source_version_id,target_version_id,relation,compatible,'
                       'old_share,new_share,overlap,evidence_digest,evidence_bytes')
            event = 'UPDATE OF ' + columns
        elif table == 'group_input_manifests':
            event = 'UPDATE OF id,content_digest'
        else:
            event = 'UPDATE'
        yield (f'CREATE TRIGGER immutable_{table} BEFORE {event} ON {table} '
               "BEGIN SELECT RAISE(ABORT,'Musical group content is immutable'); END")
    yield ('CREATE TRIGGER complete_group_version_alias BEFORE INSERT ON group_version_aliases '
           'WHEN NOT EXISTS(SELECT 1 FROM group_version_aliases WHERE external_version=NEW.external_version) '
           'AND EXISTS(SELECT 1 FROM group_versions v JOIN group_generations g ON g.id=NEW.first_generation_id '
           'JOIN group_input_manifests i ON i.id=g.input_manifest_id '
           'WHERE v.id=NEW.group_version_id AND (i.complete=0 OR '
           '(v.membership_known=1 AND v.size<>(SELECT count(*) FROM group_members WHERE group_version_id=v.id)'
           '+(SELECT count(*) FROM unresolved_group_members WHERE group_version_id=v.id)) OR '
           '(v.membership_known=0 AND (EXISTS(SELECT 1 FROM group_members WHERE group_version_id=v.id) '
           'OR EXISTS(SELECT 1 FROM unresolved_group_members WHERE group_version_id=v.id))))) '
           "BEGIN SELECT RAISE(ABORT,'Musical group version has incomplete membership'); END")
    for table in ('group_members', 'unresolved_group_members', 'group_representatives'):
        for event, side in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
            yield (f'CREATE TRIGGER sealed_{table}_{event.lower()} BEFORE {event} ON {table} '
                   'WHEN EXISTS(SELECT 1 FROM group_version_aliases WHERE group_version_id='
                   f'{side}.group_version_id) '
                   "BEGIN SELECT RAISE(ABORT,'Musical group version is sealed'); END")
    for table in ('group_manifest_inputs', 'unresolved_group_manifest_inputs'):
        for event, side in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
            yield (f'CREATE TRIGGER sealed_{table}_{event.lower()} BEFORE {event} ON {table} '
                   f'WHEN EXISTS(SELECT 1 FROM group_input_manifests WHERE id={side}.manifest_id AND complete=1) '
                   f'AND EXISTS(SELECT 1 FROM group_generations WHERE input_manifest_id={side}.manifest_id) '
                   "BEGIN SELECT RAISE(ABORT,'Musical group input manifest is sealed'); END")
    yield ('CREATE TRIGGER sealed_group_input_manifest BEFORE UPDATE OF complete ON group_input_manifests '
           "WHEN OLD.complete=1 BEGIN SELECT RAISE(ABORT,'Musical group input manifest is sealed'); END")
    for table in ('generation_groups', 'group_generation_lineage'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            condition = ' OR '.join('EXISTS(SELECT 1 FROM group_publications WHERE generation_id='
                                    + side + '.generation_id)' for side in sides)
            if table == 'group_generation_lineage' and event == 'DELETE':
                condition = '(' + condition + ') AND NOT EXISTS(SELECT 1 FROM group_lineage_retirements '
                condition += 'WHERE source_alias=OLD.source_alias AND target_alias=OLD.target_alias)'
            yield (f'CREATE TRIGGER published_{table}_{event.lower()} BEFORE {event} ON {table} '
                   f'WHEN {condition} '
                   "BEGIN SELECT RAISE(ABORT,'Published musical group selection is immutable'); END")
    yield ('CREATE TRIGGER complete_group_publication BEFORE INSERT ON group_publications WHEN '
           'EXISTS(SELECT 1 FROM group_generations g JOIN group_input_manifests i ON i.id=g.input_manifest_id '
           'WHERE g.id=NEW.generation_id AND i.complete=0) OR '
           'EXISTS(SELECT 1 FROM group_builds WHERE generation_id=NEW.generation_id AND ready=0) OR '
           'EXISTS(SELECT 1 FROM group_build_versions WHERE generation_id=NEW.generation_id) '
           "BEGIN SELECT RAISE(ABORT,'Musical group publication is incomplete'); END")
    yield ('CREATE TRIGGER immutable_group_generation BEFORE UPDATE OF id,external_key,policy_version,'
           'input_fingerprint,input_manifest_id,input_serial,created_at,metadata_digest,metadata_bytes ON group_generations '
           "BEGIN SELECT RAISE(ABORT,'Musical group generation is immutable'); END")
    yield ('CREATE TRIGGER retained_group_publication_update BEFORE UPDATE ON group_publications '
           "BEGIN SELECT RAISE(ABORT,'Retained musical group publication is immutable'); END")
    yield ('CREATE TRIGGER fenced_group_lineage_retirement BEFORE INSERT ON group_lineage_retirements WHEN '
           'NOT EXISTS(SELECT 1 FROM group_retention_state WHERE id=1 AND roots_complete=1) '
           "BEGIN SELECT RAISE(ABORT,'Musical group evidence backfill is incomplete'); END")
    yield ('CREATE TRIGGER retained_group_publication_delete BEFORE DELETE ON group_publications WHEN '
           'NOT EXISTS(SELECT 1 FROM group_retention_state WHERE id=1 AND roots_complete=1) OR '
           "NOT EXISTS(SELECT 1 FROM group_builds WHERE generation_id=OLD.generation_id AND owner='cleanup') OR "
           'EXISTS(SELECT 1 FROM group_head WHERE generation_id=OLD.generation_id) OR '
           'EXISTS(SELECT 1 FROM group_builds WHERE parent_generation_id=OLD.generation_id) OR '
           'EXISTS(SELECT 1 FROM group_evidence_generations WHERE generation_id=OLD.generation_id) OR '
           'EXISTS(SELECT 1 FROM group_evidence_versions e JOIN group_version_aliases a '
           'ON a.external_version=e.external_version WHERE a.first_generation_id=OLD.generation_id) '
           "BEGIN SELECT RAISE(ABORT,'Retained musical group publication is immutable'); END")
    for table in ('group_evidence_owners', 'group_evidence_generations', 'group_evidence_versions',
                  'unresolved_group_evidence', 'unresolved_group_contexts'):
        yield (f'CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} '
               "BEGIN SELECT RAISE(ABORT,'Musical group evidence references are immutable'); END")
    owners = ','.join("'proposal:'||OLD.surface||':" + field + "'" for field in
                      ('context', 'choice_context', 'album_choice_context'))
    deletes = ' '.join(f"DELETE FROM {table} WHERE kind IN ('prepared','comparison') AND external_key IN ({owners});"
                      for table in ('group_evidence_versions', 'group_evidence_generations', 'unresolved_group_evidence',
                                    'unresolved_group_contexts', 'group_evidence_owners'))
    yield 'CREATE TRIGGER retired_proposal_group_references BEFORE DELETE ON rec_proposals BEGIN ' + deletes + ' END'


def validate(conn):
    from .current import _tokens
    for sql in statements():
        name = sql.split()[3] if sql.startswith('CREATE UNIQUE') else sql.split()[2]
        if name == 'retained_group_publication_delete':
            from yt_playlist.core import group_retention_progress
            if group_retention_progress.available(conn):
                sql = group_retention_progress.publication_guard()
        actual = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible compact group structure: {name}')
    for table in OLD_TABLES:
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
            raise MigrationError(f'Superseded musical group owner remains: {table}')


def _prepared(conn, generation):
    key, policy, fingerprint, created, serial, payload = tuple(generation)
    _count, size = conn.execute('SELECT count(m.identity_key),COALESCE(sum(length(m.identity_key)+32),0) '
        'FROM rec_group_definitions d LEFT JOIN rec_group_members m USING(version) WHERE d.generation=?',
        (key,)).fetchone()
    definition_bytes = conn.execute('SELECT COALESCE(sum(length(CAST(payload AS BLOB))),0) '
                                    'FROM rec_group_definitions WHERE generation=?', (key,)).fetchone()[0]
    if size * 4 + definition_bytes * 4 + len(payload.encode()) * 4 > GENERATION_BYTES:
        raise ValueError('Musical group generation exceeds bounded migration memory')
    groups = []
    for version, group_id, parent, raw, legacy in conn.execute('SELECT d.version,d.group_id,d.parent_version,'
            'd.payload,i.legacy_mode_id FROM rec_group_definitions d JOIN rec_group_identities i USING(group_id) '
            'WHERE d.generation=? ORDER BY d.rowid', (key,)):
        groups.append({'version': version, 'group_id': group_id, 'parent_version': parent, 'payload': raw,
                       'legacy_mode_id': legacy, 'members': [tuple(row) for row in conn.execute(
                           'SELECT identity_key,similarity,support FROM rec_group_members WHERE version=? '
                           'ORDER BY identity_key', (version,))]})
    return {'generation': key, 'policy': policy, 'fingerprint': fingerprint, 'at': created,
            'serial': serial, 'payload': payload, 'groups': groups}


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Invalid musical group timestamp')


def _lineage(raw):
    value = group_content.document(raw, 'Musical group lineage')
    for field in ('old_share', 'new_share'):
        if value.get(field) is not None:
            group_content.score(value[field])
    if value.get('compatible') is not None and type(value['compatible']) is not bool:
        raise ValueError('Invalid musical group lineage compatibility')
    if value.get('overlap') is not None and (type(value['overlap']) is not int or value['overlap'] < 0):
        raise ValueError('Invalid musical group lineage overlap')
    return value


def _preflight(conn, resolve):
    incomplete_versions = set()
    for table in OLD_TABLES:
        if conn.execute(f'PRAGMA foreign_key_check({table})').fetchone() is not None:
            raise ValueError(f'Broken original musical group reference in {table}')
    for key, legacy, created in conn.execute('SELECT * FROM rec_group_identities'):
        if not isinstance(key, str) or not key or legacy is not None and type(legacy) is not int:
            raise ValueError('Invalid musical group identity')
        _time(created)
    for generation in conn.execute('SELECT * FROM rec_group_generations ORDER BY rowid'):
        prepared = _prepared(conn, generation)
        published = conn.execute('SELECT 1 FROM rec_group_publications WHERE generation=?',
                                 (prepared['generation'],)).fetchone() is not None
        build = conn.execute('SELECT ready FROM rec_group_builds WHERE generation=?',
                             (prepared['generation'],)).fetchone()
        if not published and build is None:
            raise ValueError('Unpublished musical group generation has no build lease')
        if (not isinstance(prepared['generation'], str) or not prepared['generation']
                or not isinstance(prepared['fingerprint'], str) or type(prepared['serial']) is not int
                or prepared['serial'] < 0):
            raise ValueError('Invalid musical group generation metadata')
        _time(prepared['at'])
        payload = group_content.document(prepared['payload'], 'Musical group generation')
        group_content.input_manifest(payload.get('recording_inputs', {}))
        group_content.document(prepared['policy'], 'Musical group policy')
        staged_digest = payload.get('_staging_digest')
        if staged_digest is not None and (not isinstance(staged_digest, str) or len(staged_digest) != 64
                or any(c not in '0123456789abcdef' for c in staged_digest)):
            raise ValueError('Invalid musical group staging digest')
        parent = payload.get('_staging_parent')
        if parent is not None and (not isinstance(parent, str) or not conn.execute(
                'SELECT 1 FROM rec_group_generations WHERE generation=?', (parent,)).fetchone()):
            raise ValueError('Missing musical group staging parent')
        parents = {group['version']: group.get('parent_version') for group in prepared['groups']}
        for version in parents:
            visited, parent = {version}, parents[version]
            while parent is not None:
                if parent in visited or parent not in parents:
                    raise ValueError('Invalid musical group parent graph')
                visited.add(parent)
                parent = parents[parent]
        for group in prepared['groups']:
            if not isinstance(group['version'], str) or not group['version']:
                raise ValueError('Invalid musical group external version')
            value = group_content.document(group['payload'], 'Musical group definition')
            unfinished = not published and not build[0]
            fields = group_storage.definition_fields(value, None if unfinished else group['members'])
            if not fields[2] and group['members']:
                raise ValueError('Unknown musical group membership has asserted members')
            if fields[2] and fields[4] != len(group['members']):
                if len(group['members']) > fields[4]:
                    raise ValueError('Unfinished musical group membership exceeds its declared size')
                incomplete_versions.add(group['version'])
            group_content.digest(group_content.prepared_rows(prepared, group, resolve))
    for source, target, relation, payload in conn.execute('SELECT * FROM rec_group_lineage'):
        _lineage(payload)
        if (not isinstance(source, str) or not source or not isinstance(target, str) or not target
                or relation not in ('continuation', 'split', 'merge', 'legacy')):
            raise ValueError('Invalid musical group lineage relation')
        if not all(conn.execute('SELECT 1 FROM rec_group_definitions WHERE version=?', (version,)).fetchone()
                   for version in (source, target)):
            raise ValueError('Missing musical group lineage version')
        if source in incomplete_versions or target in incomplete_versions:
            raise ValueError('Temporal musical group evidence refers to incomplete membership')
    for generation, owner, expires, ready in conn.execute('SELECT * FROM rec_group_builds'):
        if not isinstance(owner, str) or not owner or type(ready) is not int or ready not in (0, 1):
            raise ValueError('Invalid musical group build lease')
        _time(expires)
    for generation, left, right, weight, support in conn.execute('SELECT * FROM rec_group_graph'):
        if not isinstance(left, str) or not isinstance(right, str):
            raise ValueError('Invalid musical group graph key')
        group_content.score(weight)
        group_content.score(support)
    for key, generation, version, similarity, support, profile, prepared_at in conn.execute(
            'SELECT * FROM rec_group_assignments'):
        if not isinstance(key, str) or profile is not None and not isinstance(profile, str):
            raise ValueError('Invalid musical group assignment')
        if version is not None and not conn.execute('SELECT 1 FROM rec_group_definitions '
                'WHERE generation=? AND version=?', (generation, version)).fetchone():
            raise ValueError('Musical group assignment version belongs to another generation')
        if version in incomplete_versions:
            raise ValueError('Musical group assignment refers to incomplete membership')
        group_content.score(similarity)
        group_content.score(support)
        _time(prepared_at)
    for _generation, key, reason in conn.execute('SELECT * FROM rec_group_unknown'):
        if not isinstance(key, str) or not isinstance(reason, str):
            raise ValueError('Invalid musical group unresolved evidence')


def apply(conn, context):
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise MigrationError('Compact group replacement requires the migration runner foreign-key fence')
    # Exact identity_key lookup is the only accepted historical association.
    recording_ids = dict(conn.execute('SELECT identity_key,id FROM recordings'))
    resolve = recording_ids.get
    try:
        _preflight(conn, resolve)
    except (ValueError, TypeError, KeyError) as exc:
        raise MigrationError(f'Invalid original musical group state: {exc}') from exc
    for sql in statements():
        if not sql.startswith('CREATE TRIGGER'):
            conn.execute(sql)
    conn.execute('INSERT INTO group_retention_state VALUES (1,0)')
    conn.execute('INSERT INTO taste_groups(external_key,legacy_mode_id,created_at) '
                 'SELECT group_id,legacy_mode_id,created_at FROM rec_group_identities ORDER BY rowid')
    for generation in conn.execute('SELECT * FROM rec_group_generations ORDER BY rowid'):
        key, raw_policy, fingerprint, created, serial, raw = tuple(generation)
        policy = group_content.document(raw_policy, 'Musical group policy')
        policy_version = group_storage.policy_version(raw_policy)
        policy_documents.install(conn, [(policy_version, canonical(policy).decode(), created)])
        metadata = group_content.document(raw, 'Musical group generation')
        manifest_id = group_storage.install_manifest(conn, metadata.pop('recording_inputs', {}), resolve)
        build = conn.execute('SELECT owner,expires_at,ready FROM rec_group_builds WHERE generation=?', (key,)).fetchone()
        if build is not None:
            metadata['_legacy_build'] = dict(build)
        filename, digest, size, offset = next(group_documents.publish_many(
            group_documents.directory(conn), [('generation', metadata)]))
        conn.execute('INSERT INTO group_generations(external_key,policy_version,input_fingerprint,input_manifest_id,'
                     'input_serial,created_at,metadata_filename,metadata_digest,metadata_bytes,metadata_offset) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (key, policy_version, fingerprint, manifest_id, serial, created, filename, digest, size, offset))
    generation_ids = dict(conn.execute('SELECT external_key,id FROM group_generations'))
    for generation, owner, expires, ready in conn.execute('SELECT * FROM rec_group_builds'):
        # Old staged output may be a partial member prefix with no full content
        # digest. Retain it and its lease expiry, then clean it before rebuilding;
        # no current worker can claim that private prefix as complete content.
        metadata = json.loads(conn.execute('SELECT payload FROM rec_group_generations WHERE generation=?',
                                           (generation,)).fetchone()[0])
        parent = metadata.get('_staging_parent')
        # The original publication map is still present during this copy.
        parent_id = generation_ids[parent] if parent is not None and conn.execute(
            'SELECT 1 FROM rec_group_publications WHERE generation=?', (parent,)).fetchone() else None
        conn.execute("INSERT INTO group_builds VALUES (?,'cleanup',?,0,?,?)",
                     (generation_ids[generation], expires, metadata.get('_staging_digest'), parent_id))
    for generation in conn.execute('SELECT * FROM rec_group_generations ORDER BY rowid'):
        prepared = _prepared(conn, generation)
        unfinished = conn.execute('SELECT 1 FROM rec_group_builds WHERE generation=? AND ready=0',
                                   (prepared['generation'],)).fetchone() is not None
        group_storage.install_versions(conn, prepared, generation_ids[prepared['generation']], resolve,
                                        unfinished_legacy=unfinished)
    lineage = conn.execute('SELECT * FROM rec_group_lineage ORDER BY source,target')
    pending, size = [], 0
    def copy_lineage():
        for (source, target, relation, payload), file in zip(pending, group_documents.publish_many(
                group_documents.directory(conn), (('lineage', _lineage(row[3])) for row in pending))):
            source_id = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                                     (source,)).fetchone()[0]
            target_id = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                                     (target,)).fetchone()[0]
            value = _lineage(payload)
            compatible = int(value['compatible']) if value.get('compatible') is not None else None
            conn.execute('INSERT INTO group_lineage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (source, target, source_id, target_id, relation, compatible, value.get('old_share'),
                          value.get('new_share'), value.get('overlap'), *file))
            generation = conn.execute('SELECT generation FROM rec_group_definitions WHERE version=?', (target,)).fetchone()[0]
            conn.execute('INSERT INTO group_generation_lineage VALUES (?,?,?)',
                         (generation_ids[generation], source, target))
    for row in lineage:
        used = len(row[3].encode())
        if pending and size + used > group_documents.PACK_BYTES:
            copy_lineage()
            pending, size = [], 0
        pending.append(tuple(row))
        size += used
    if pending:
        copy_lineage()
    for generation, in conn.execute('SELECT generation FROM rec_group_publications'):
        conn.execute('INSERT INTO group_publications VALUES (?,NULL)', (generation_ids[generation],))
    for _id, generation in conn.execute('SELECT * FROM rec_group_head'):
        conn.execute('INSERT INTO group_head VALUES (1,?)', (generation_ids[generation],))
    for generation, key, reason in conn.execute('SELECT * FROM rec_group_unknown'):
        owner = resolve(key)
        if owner is None:
            conn.execute("INSERT INTO unresolved_group_unknown VALUES (?,?,?,'missing_recording')",
                         (generation_ids[generation], key, reason))
        else:
            conn.execute('INSERT INTO group_unknown VALUES (?,?,?)', (generation_ids[generation], owner, reason))
    for generation, left, right, weight, support in conn.execute('SELECT * FROM rec_group_graph'):
        left_id, right_id = resolve(left), resolve(right)
        if left_id is None or right_id is None:
            conn.execute("INSERT INTO unresolved_group_graph VALUES (?,?,?,?,?,'missing_recording')",
                         (generation_ids[generation], left, right, weight, support))
        else:
            conn.execute('INSERT INTO group_graph VALUES (?,?,?,?,?)',
                         (generation_ids[generation], left_id, right_id, weight, support))
    for key, generation, version, similarity, support, profile, prepared_at in conn.execute(
            'SELECT * FROM rec_group_assignments'):
        owner = resolve(key)
        version_id = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                                  (version,)).fetchone()[0] if version is not None else None
        table = 'group_assignments' if owner is not None else 'unresolved_group_assignments'
        key_column = 'recording_id' if owner is not None else 'raw_key'
        conn.execute(f'INSERT INTO {table}(generation_id,{key_column},group_version_id,similarity,support,'
                     'profile_digest,prepared_at) VALUES (?,?,?,?,?,?,?)',
                     (generation_ids[generation], owner if owner is not None else key, version_id,
                      similarity, support, profile, prepared_at))
    conn.execute('DROP VIEW rec_group_edges')
    # The runner reserves the writer, fences foreign keys before BEGIN and
    # checks all references before commit. No live application sees this prefix.
    for table in OLD_TABLES:
        conn.execute(f'DROP TABLE {table}')
    for sql in statements():
        if sql.startswith('CREATE TRIGGER'):
            conn.execute(sql)
