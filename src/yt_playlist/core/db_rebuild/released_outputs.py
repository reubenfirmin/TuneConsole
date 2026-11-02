"""Released policy documents and prepared output use their canonical writers."""
from functools import partial
import json

from .. import (album_note_outputs, album_note_requests, choice_learning_state, home_offers,
               home_outputs, listening_outputs, policy_documents, recommendation_reports,
               recommendation_requests, recommendation_work)
from ..migrations import (v070_policy_documents as policies, v071_album_descriptions as descriptions,
                          v073_recommendation_publications as publications)
from ...repos import recommendation_publication
from . import development, source
from .build import Batch, Stage


CONVERTED = frozenset({'rec_serving_policies','rec_policy_bases','rec_policy_variants',
    'rec_proposal_cache_builds','rec_proposal_cache_chunks','rec_proposal_cache_heads'})


def _policy(reader, target, cursor, limit, *, table):
    rows,bookmark,done = source.scan(reader,table,cursor,limit)
    prepared = []
    for row in rows:
        key = row['base_version' if table=='rec_policy_bases' else 'model_version']
        policies._metadata(key,row['created_at'])
        value = policies._configuration(row['configuration'],key)
        if table=='rec_policy_bases' and (row['byte_size']!=len(row['configuration'].encode())
                or type(row['served']) is not int or row['served'] not in (0,1)):
            raise ValueError('Invalid original shared policy: '+key)
        if table=='rec_serving_policies':
            base = value.get(policies.BASE_FIELD)
            link = reader.execute('SELECT base_version FROM rec_policy_variants WHERE model_version=?',(key,)).fetchone()
            if (base is not None and not target.execute('SELECT 1 FROM recommendation_policy_bodies WHERE version=?',
                    (base,)).fetchone()) or link and link[0]!=base:
                raise ValueError('Invalid original policy family: '+key)
        prepared.append((key,value,row['created_at']))
    for (key,value,at), file in zip(prepared,policy_documents.publish_many(
            policy_documents.directory(target),(value for _,value,_ in prepared))):
        if table=='rec_policy_bases':
            target.execute('INSERT INTO recommendation_policy_bodies VALUES (?,?,?,?,?,?,0)',(key,*file,at))
        else:
            target.execute('INSERT INTO recommendation_policies VALUES (?,?,?,?,?,?,?,0)',
                           (key,value.get(policies.BASE_FIELD),*file,at))
    return Batch(bookmark,len(rows),done)


def _links(reader, target, cursor, limit):
    values,bookmark,done = source.scan(reader,'rec_served_impressions',cursor,limit)
    for row in values:
        if not isinstance(row['model_version'],str) or not isinstance(json.loads(row['provenance']),dict):
            raise ValueError('Invalid original served policy evidence: '+str(row['id']))
        target.execute(policies._link_sql('i.id=?'),(row['id'],row['id']))
    if done:
        orphan = reader.execute('SELECT v.model_version FROM rec_policy_variants v '
            'LEFT JOIN rec_serving_policies p ON p.model_version=v.model_version '
            'LEFT JOIN rec_policy_bases b ON b.base_version=v.base_version '
            'WHERE p.model_version IS NULL OR b.base_version IS NULL LIMIT 1').fetchone()
        if orphan:
            raise ValueError('Orphan original policy variant: '+orphan[0])
        target.execute('UPDATE recommendation_policies SET served=EXISTS(SELECT 1 FROM '
            'recommendation_impression_policies WHERE policy_version=recommendation_policies.version)')
        target.execute('UPDATE recommendation_policy_bodies SET served=EXISTS(SELECT 1 FROM '
            'recommendation_policies WHERE base_version=recommendation_policy_bodies.version AND served=1)')
    return Batch(bookmark,len(values),done)


def _bundle(reader, target, cursor, limit):
    plan = publications._legacy_plan(reader,destination=target)
    if plan is not None:
        recommendation_publication.import_plan(target,plan,plan.created_at)
    return Batch(done=True)


def _listening(reader, target, cursor, limit):
    # The owner validates its complete bounded edition and chunk index together.
    predicate = ("surface IN ('listening_analytics','listening_edition') "
                 "OR substr(surface,1,16)='listening_chunk:'")
    count,size = reader.execute('SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) '
                                'FROM rec_proposals WHERE '+predicate).fetchone()
    if count>listening_outputs.MAX_CHUNKS+2 or size>listening_outputs.MAX_PUBLICATION_BYTES:
        raise ValueError('Original listening publication exceeds its bounded admission')
    rows = {surface:(raw,at) for surface,raw,at in reader.execute(
        'SELECT surface,payload,built_at FROM rec_proposals WHERE '+predicate)}
    if rows:
        plan = listening_outputs.prepare({k.removeprefix('listening_chunk:'):v[0] for k,v in rows.items()
                if k.startswith('listening_chunk:')},rows.get('listening_analytics',(None,None))[0],
            rows.get('listening_edition',(None,None))[0],rows.get('listening_analytics',(None,None))[1],
            historical=True,output_times={k:v[1] for k,v in rows.items()})
        listening_outputs.publish(target,plan)
    return Batch(rows=len(rows),done=True)


def proposal(reader, target, row):
    surface,raw,at = row['surface'],row['payload'],row['built_at']
    if surface in ('mode_bundles','fresh_pool','listening_analytics','listening_edition') or surface.startswith('listening_chunk:'):
        return  # Their complete bounded publication is installed separately.
    if surface.startswith((descriptions.RESULT_PREFIX,descriptions.CREDENTIAL_PREFIX)) or surface in descriptions.LEGACY_KEYS:
        descriptions.copy_rows(target,[(surface,raw,at)],access_overlay=False)
    elif surface in ('daily_model_schedule','tv_preparation'):
        recommendation_work.write(target,surface,json.loads(raw),at)
    elif surface=='home_shown':
        home_offers.replace(target,json.loads(raw),at)
    elif surface=='album_note_requests':
        album_note_requests.replace(target,json.loads(raw),at,historical=True)
    elif surface=='album_notes':
        for browse,filename,digest,size in album_note_outputs.prepare(target,json.loads(raw),at):
            album_note_outputs._insert(target,browse,filename,digest,size,at)
    elif surface in recommendation_requests.SURFACES:
        recommendation_requests.write(target,surface,json.loads(raw),at)
    elif surface in home_outputs.SURFACES:
        plan = home_outputs.prepare(surface,raw,at)
        home_outputs.policy_references(plan.value)
        home_outputs.publish(target,plan,historical=True)
    elif surface in choice_learning_state.SURFACES:
        choice_learning_state.write(target,surface,choice_learning_state.decode(surface,raw),at,historical=True)
    elif surface in recommendation_reports.SURFACES:
        recommendation_reports.publish(target,recommendation_reports.prepare(surface,raw,at))
    else:
        development.proposal(reader,target,row)


def policy_stages():
    for table in ('rec_policy_bases','rec_serving_policies'):
        yield Stage('policy:'+table,partial(_policy,table=table))
    yield Stage('policy_impressions',_links)


def output_stages():
    yield Stage('recommendation_bundle',_bundle)
    yield Stage('listening_output',_listening)
    yield development.rows('rec_proposals',proposal)
    def access_waits(reader,target,cursor,limit):
        descriptions.copy_access_waits(target)
        return Batch(done=True)
    yield Stage('paid_description_access_waits',access_waits)
