"""Import released group generations without a library-sized identity map."""
from functools import partial
import json
from pathlib import Path

from .. import group_content, group_documents, group_storage, policy_documents
from ..evidence_ledger import canonical
from . import development, source, released_group_inputs as legacy
from .build import Batch, Stage


CONVERTED = frozenset(legacy.OLD_TABLES)


def _recording(target, key):
    row = target.execute('SELECT id FROM recordings WHERE identity_key=?',(key,)).fetchone()
    return row[0] if row else None


def _generation_id(target, key):
    row = target.execute('SELECT id FROM group_generations WHERE external_key=?',(key,)).fetchone()
    if row is None:
        raise ValueError('Missing retained group generation: '+str(key))
    return row[0]


def _version_id(target, key):
    row = target.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',(key,)).fetchone()
    if row is None:
        raise ValueError('Missing retained group version: '+str(key))
    return row[0]


def _preflight(reader, target, cursor, limit):
    legacy._preflight(reader,partial(_recording,target))
    return Batch(done=True)


def _identity(reader, target, row):
    target.execute('INSERT INTO taste_groups(external_key,legacy_mode_id,created_at) VALUES (?,?,?)',
                   (row['group_id'],row['legacy_mode_id'],row['created_at']))


def _generation(reader, target, row):
    key,raw_policy,fingerprint,created,serial,raw = tuple(row.values())
    policy = group_content.document(raw_policy,'Musical group policy')
    version = group_storage.policy_version(raw_policy)
    policy_documents.install(target,[(version,canonical(policy).decode(),created)])
    metadata = group_content.document(raw,'Musical group generation')
    manifest = group_storage.install_manifest(target,metadata.pop('recording_inputs',{}),partial(_recording,target))
    build = reader.execute('SELECT owner,expires_at,ready FROM rec_group_builds WHERE generation=?',(key,)).fetchone()
    if build:
        metadata['_legacy_build'] = dict(build)
    file = next(group_documents.publish_many(group_documents.directory(target),[('generation',metadata)]))
    target.execute('INSERT INTO group_generations(external_key,policy_version,input_fingerprint,input_manifest_id,'
        'input_serial,created_at,metadata_filename,metadata_digest,metadata_bytes,metadata_offset) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',(key,version,fingerprint,manifest,serial,created,*file))


def _build(reader, target, row):
    key = row['generation']
    metadata = json.loads(reader.execute('SELECT payload FROM rec_group_generations WHERE generation=?',(key,)).fetchone()[0])
    parent = metadata.get('_staging_parent')
    parent_id = _generation_id(target,parent) if parent is not None and reader.execute(
        'SELECT 1 FROM rec_group_publications WHERE generation=?',(parent,)).fetchone() else None
    target.execute("INSERT INTO group_builds VALUES (?,'cleanup',?,0,?,?)",
        (_generation_id(target,key),row['expires_at'],metadata.get('_staging_digest'),parent_id))


def _versions(reader, target, cursor, limit, *, sparse=False):
    # The existing group owner enforces a 64 MiB budget for one generation.
    rows,bookmark,done = source.scan(reader,'rec_group_generations',cursor,1,
        predicate=retained(reader)['rec_group_generations'] if sparse else None)
    for row in rows:
        prepared = legacy._prepared(reader,tuple(row.values()))
        unfinished = reader.execute('SELECT 1 FROM rec_group_builds WHERE generation=? AND ready=0',
                                     (row['generation'],)).fetchone() is not None
        group_storage.install_versions(target,prepared,_generation_id(target,row['generation']),
            partial(_recording,target),unfinished_legacy=unfinished)
    return Batch(bookmark,len(rows),done)


def _lineage(reader, target, row):
    left,right,relation,raw = tuple(row.values())
    value = legacy._lineage(raw)
    file = next(group_documents.publish_many(group_documents.directory(target),[('lineage',value)]))
    compatible = int(value['compatible']) if value.get('compatible') is not None else None
    target.execute('INSERT INTO group_lineage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (left,right,_version_id(target,left),_version_id(target,right),relation,compatible,
         value.get('old_share'),value.get('new_share'),value.get('overlap'),*file))
    generation = reader.execute('SELECT generation FROM rec_group_definitions WHERE version=?',(right,)).fetchone()[0]
    target.execute('INSERT INTO group_generation_lineage VALUES (?,?,?)',(_generation_id(target,generation),left,right))


def _publication(reader, target, row):
    target.execute('INSERT INTO group_publications VALUES (?,NULL)',(_generation_id(target,row['generation']),))


def _head(reader, target, row):
    target.execute('INSERT INTO group_head VALUES (1,?)',(_generation_id(target,row['generation']),))


def _unknown(reader, target, row):
    generation,key,reason = tuple(row.values())
    owner = _recording(target,key)
    generation = _generation_id(target,generation)
    if owner is None:
        target.execute("INSERT INTO unresolved_group_unknown VALUES (?,?,?,'missing_recording')",(generation,key,reason))
    else:
        target.execute('INSERT INTO group_unknown VALUES (?,?,?)',(generation,owner,reason))


def _graph(reader, target, row):
    generation,left,right,weight,support = tuple(row.values())
    generation = _generation_id(target,generation)
    left_id,right_id = _recording(target,left),_recording(target,right)
    if left_id is None or right_id is None:
        target.execute("INSERT INTO unresolved_group_graph VALUES (?,?,?,?,?,'missing_recording')",
                       (generation,left,right,weight,support))
    else:
        target.execute('INSERT INTO group_graph VALUES (?,?,?,?,?)',(generation,left_id,right_id,weight,support))


def _assignment(reader, target, row):
    key,generation,version,similarity,support,profile,at = tuple(row.values())
    owner = _recording(target,key)
    value = dict(generation_id=_generation_id(target,generation),
        group_version_id=_version_id(target,version) if version is not None else None,
        similarity=similarity,support=support,profile_digest=profile,prepared_at=at)
    table = 'group_assignments' if owner is not None else 'unresolved_group_assignments'
    value['recording_id' if owner is not None else 'raw_key'] = owner if owner is not None else key
    development.insert(target,table,value)


def retained(reader):
    """Select current released groups before reading their bulky documents."""
    if not reader.execute("SELECT 1 FROM temp.sqlite_schema WHERE name='runtime_released_generations'").fetchone():
        reader.execute('CREATE TEMP TABLE runtime_released_generations(id TEXT PRIMARY KEY) WITHOUT ROWID')
        reader.execute('CREATE TEMP TABLE runtime_released_versions(id TEXT PRIMARY KEY) WITHOUT ROWID')
        reader.execute('CREATE TEMP TABLE runtime_released_edges(source TEXT,target TEXT,PRIMARY KEY(source,target)) WITHOUT ROWID')
        at = Path(reader.execute('PRAGMA database_list').fetchone()[2]).stat().st_mtime
        reader.execute('INSERT INTO runtime_released_generations SELECT generation FROM rec_group_head '
            "UNION SELECT generation FROM rec_group_builds WHERE owner<>'cleanup' AND expires_at>=?", (at,))
        checkpoint = reader.execute("SELECT value FROM settings WHERE key='rec_group_shadow_checkpoint'").fetchone()
        if checkpoint and checkpoint[0]:
            generation = json.loads(checkpoint[0]).get('generation')
            reader.execute('INSERT OR IGNORE INTO runtime_released_generations SELECT generation '
                           'FROM rec_group_generations WHERE generation=?',(generation,))
        # Current output contexts are serving dependencies. Historical choice
        # menus stay in the immutable ledger, without pinning the old graph.
        from .. import evidence_ledger, group_evidence, home_outputs, prepared_references
        from ...rec import comparison_storage
        ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(reader),2**63-1)
        for surface,raw in reader.execute('SELECT surface,payload FROM rec_proposals'):
            if surface not in prepared_references.PREPARED and not surface.startswith('model_pair:'):
                continue
            value = (home_outputs.prepare(surface,raw,0.).value if surface in home_outputs.SURFACES
                     else comparison_storage.decode(json.loads(raw)))
            value = value if isinstance(value,dict) else {}
            contexts = [value.get('context')]
            if surface == 'adaptive_group_preview':
                contexts.append({'group_generation':value.get('generation')})
            for context in contexts:
                if isinstance(context,dict):
                    try:
                        context = prepared_references.context(None,context,ledger=ledger)
                    except ValueError:
                        continue  # Importer retains this exact unresolved context.
                    generation, versions = group_evidence.references(context)
                    if generation is not None:
                        reader.execute('INSERT OR IGNORE INTO runtime_released_generations SELECT generation '
                            'FROM rec_group_generations WHERE generation=?',(generation,))
                    for version,_ in versions:
                        reader.execute('INSERT OR IGNORE INTO runtime_released_versions SELECT version '
                            'FROM rec_group_definitions WHERE version=?',(version,))
        while True:
            before = reader.total_changes
            for row in reader.execute('SELECT g.payload FROM rec_group_generations g JOIN rec_group_builds b '
                'USING(generation) WHERE g.generation IN (SELECT id FROM runtime_released_generations) '
                "AND b.owner<>'cleanup' AND b.expires_at>=?",(at,)).fetchall():
                parent = json.loads(row[0]).get('_staging_parent')
                if parent is not None:
                    reader.execute('INSERT OR IGNORE INTO runtime_released_generations SELECT generation '
                                   'FROM rec_group_publications WHERE generation=?',(parent,))
            reader.execute('INSERT OR IGNORE INTO runtime_released_versions SELECT version FROM rec_group_definitions '
                           'WHERE generation IN (SELECT id FROM runtime_released_generations)')
            reader.execute('INSERT OR IGNORE INTO runtime_released_edges SELECT l.source,l.target '
                'FROM rec_group_lineage l JOIN rec_group_definitions d ON d.version=l.target '
                'WHERE l.source IN (SELECT id FROM runtime_released_versions) '
                'AND (EXISTS(SELECT 1 FROM rec_group_publications p WHERE p.generation=d.generation) '
                "OR EXISTS(SELECT 1 FROM rec_group_builds b WHERE b.generation=d.generation AND b.owner<>'cleanup' AND b.expires_at>=?))", (at,))
            reader.execute('INSERT OR IGNORE INTO runtime_released_versions SELECT source FROM runtime_released_edges '
                           'UNION SELECT target FROM runtime_released_edges')
            reader.execute('INSERT OR IGNORE INTO runtime_released_generations SELECT generation '
                           'FROM rec_group_definitions WHERE version IN (SELECT id FROM runtime_released_versions)')
            if reader.total_changes == before:
                break
    predicates = {table:'s.generation IN (SELECT id FROM runtime_released_generations)' for table in (
        'rec_group_generations','rec_group_builds','rec_group_publications','rec_group_head',
        'rec_group_unknown','rec_group_graph','rec_group_assignments')}
    predicates['rec_group_lineage'] = '(s.source,s.target) IN (SELECT * FROM runtime_released_edges)'
    predicates['rec_group_identities'] = ('s.group_id IN (SELECT group_id FROM rec_group_definitions '
        'WHERE generation IN (SELECT id FROM runtime_released_generations))')
    return predicates


def stages(*, sparse=False):
    if not sparse:
        yield Stage('released_group_preflight',_preflight)
    for table,convert in (('rec_group_identities',_identity),('rec_group_generations',_generation),
            ('rec_group_builds',_build)):
        yield development.rows(table,convert,predicate=(lambda reader,table=table:retained(reader).get(table)) if sparse else None)
    yield Stage('released_group_versions',partial(_versions,sparse=sparse))
    for table,convert in (('rec_group_lineage',_lineage),('rec_group_publications',_publication),
            ('rec_group_head',_head),('rec_group_unknown',_unknown),('rec_group_graph',_graph),
            ('rec_group_assignments',_assignment)):
        yield development.rows(table,convert,predicate=(lambda reader,table=table:retained(reader).get(table)) if sparse else None)
