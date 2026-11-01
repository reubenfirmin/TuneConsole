"""Direct imports for the explicitly supported development layout (schema 93).

These callbacks use the same domain writers as the historical conversions.
They neither replace source tables nor commit independently of the rebuild.
"""
import json
import sqlite3

from .. import (action_work, album_tracklists, append_work, collection_recovery,
               experiment_decision_state, legacy_evidence, playlist_recipe_state,
               recommendation_setting_outputs, road_trip_draft_storage,
               spotify_job_state, taste_control_state, workflow_settings)
from ..migrations import v094_collection_state as collections
from ..migrations import v096_taste_controls as taste
from ..migrations import v101_discovery_subjects as discovery
from ..migrations.runner import Context
from ..preparation.listening_subjects import describe as listening_subject
from .build import Batch, Stage, quote
from . import source


CONVERTED = frozenset({'actions','album_tracks','album_fetch_state','album_share_snapshots',
    'artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',
    'discovery_visible_impressions','play_events','listening_observations',
    'rec_recipes','cluster_canvas','road_trip_drafts','settings','rec_proposals'})
# This cache has no unique source evidence; current listening summaries derive
# from the copied events, observations and calendars, with their original IDs.
DERIVED = frozenset({'trend_first_play'})


def insert(conn, table, value):
    conn.execute('INSERT INTO '+quote(table)+'('+','.join(map(quote,value))+') VALUES ('+
                 ','.join('?' for _ in value)+')', tuple(value.values()))


def rows(table, convert, *, name=None):
    def copy(reader, target, cursor, limit):
        values, cursor, done = source.scan(reader, table, cursor, limit)
        for value in values:
            convert(reader, target, value)
        return Batch(cursor,len(values),done)
    return Stage(name or table,copy)


def _listening(reader, target, row, *, table, kind):
    # This owner deliberately leaves uncaptured membership context unknown.
    value = listening_subject(reader, kind, row)
    value.pop('source_snapshot')
    insert(target,table,value)


def _action(reader, target, row):
    action_work.insert(target,row['kind'],row['params_json'],row['plan_json'],row['status'],
        row['undo_json'],row['created_at'],identity=row['id'],executed_at=row['executed_at'],
        idempotency_key='legacy-action:'+str(row['id']),legacy=True)


def _recipe(reader, target, row):
    value = json.loads(row['recipe'])
    playlist_recipe_state.save(target,row['playlist_ytm'],value,row['created_at'])
    recipe = target.execute('SELECT id,playlist_scope_id FROM rec_recipes ORDER BY id DESC LIMIT 1').fetchone()
    append_work.save_tv(target,recipe['id'],recipe['playlist_scope_id'],value)


def _canvas(reader, target, row):
    playlist_recipe_state.save_canvas(target,row['playlist_ytm'],row['state'],row['created_at'])


def _draft(reader, target, row):
    road_trip_draft_storage.save(target,row['recipe_id'],json.loads(row['state']),row['updated_at'],legacy=True)


def settings(reader, target, cursor, limit):
    """Convert fixed-size owner groups together; prefix owners remain batched."""
    if cursor is None:
        original = source.settings(reader,collections.KEYS)
        collections.copy_settings(target,original,Context(0.))
        saved = source.settings(reader,taste.KEYS)
        taste_control_state.put(target,{k:v for k,v in saved.items() if k in taste_control_state.KEYS},legacy=True)
        experiment_decision_state.put(target,{k:v for k,v in saved.items() if k in experiment_decision_state.KEYS},legacy=True)
        jobs = source.settings(reader,{'spotify_library_import_job'})
        if jobs.get('spotify_library_import_job'):
            spotify_job_state.save(target,json.loads(jobs['spotify_library_import_job']),legacy=True)
        recommendation_setting_outputs.put(target,source.settings(reader,{'radio_seed_offer','rec_autotune_result'}))
        from .. import radio_policy_roots
        for row in target.execute('SELECT * FROM radio_sessions').fetchall():
            state = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                            kind='radio-session',owner=row['id'])
            append_work.save_radio(target,row['id'],row['revision'],state)
            radio_policy_roots.save_session(target,row['id'],state)
        return Batch(['rows',None],len(original)+len(saved)+len(jobs),False)
    values, bookmark, done = source.scan(reader,'settings',cursor[1],limit)
    for row in values:
        key,value = row['key'],row['value']
        if key in collections.KEYS or key in taste.KEYS or key in {
                'spotify_library_import_job','radio_seed_offer','rec_autotune_result',
                'trend_first_play_watermark','trend_first_play_source'}:
            continue
        if key.startswith(collections.PREFIXES):
            collections.copy_settings(target,{key:value},Context(0.))
        elif workflow_settings.owns(key):
            workflow_settings.put(target,{key:value},legacy=True)
        else:
            target.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key,value))
    return Batch(['rows',bookmark],len(values),done)


def proposal(reader, target, row):
    from .. import library_pool_state, model_comparison_state, model_projections, tv_recommendation_state
    from ...repos import recommendation_publication
    surface,raw,at = row['surface'],row['payload'],row['built_at']
    if surface in tv_recommendation_state.SURFACES:
        tv_recommendation_state.write(target,surface,tv_recommendation_state.decode(surface,raw),at,historical=True)
    elif surface in library_pool_state.SURFACES:
        value = library_pool_state.decode(surface,raw)
        plan = library_pool_state.prepare(target,surface,value,at)
        if plan is not None:
            recommendation_publication.preflight(target,plan)
            recommendation_publication.import_plan(target,plan,plan.created_at,
                before_publish=lambda generation:library_pool_state.parameters(target,generation,value['parameters'],at))
    elif surface in model_comparison_state.SURFACES:
        model_comparison_state.write(target,surface,model_comparison_state.decode(surface,raw),at)
    elif surface.startswith('model_pair:'):
        prepared = model_projections.prepare(target,surface,json.loads(raw),at,historical=True)
        if prepared.plan is not None:
            recommendation_publication.preflight(target,prepared.plan)
        model_projections.publish(target,prepared,historical=True)
        from .. import group_evidence
        for field in (*model_projections.CONTEXTS,'recommendations'):
            key = 'proposal:'+surface+':'+field
            group_evidence.release(target,'comparison',key)
            recommendation_publication.release(target,'comparison',key)
    else:
        raise ValueError('Unclassified recommendation surface: '+surface)


def _discovery(reader, target, row, *, table):
    # Retain exact captured input, including display fields absent from the
    # typed destination. The immutable original remains separate recovery too.
    domain = 'artist' if table in ('artist_scans','discovered_artists') else 'discovery'
    key = json.dumps(row,sort_keys=True,separators=(',',':'))
    import hashlib
    legacy_evidence.retain_many(target,domain,[('discovery_cutover:'+table,
        hashlib.sha256(key.encode()).hexdigest(),key,'direct rebuild retained discovery input')])
    discovery.copy_row(target,table,row)


def _offers(reader, target, row, *, kind):
    discovery.copy_offers(target,kind,row)


def album_tracks(reader, target, cursor, limit):
    """Publish one release in bounded occurrence batches, preserving order gaps."""
    if cursor is None or cursor['phase']=='next':
        after = cursor['album'] if cursor else None
        query = ('SELECT album_id FROM (SELECT album_id FROM album_tracks WHERE is_full=1 '
                 'UNION SELECT album_id FROM album_fetch_state)')
        query += ' WHERE album_id>? ORDER BY album_id LIMIT 1' if after is not None else ' ORDER BY album_id LIMIT 1'
        row = reader.execute(query,(after,) if after is not None else ()).fetchone()
        if row is None:
            return Batch(done=True)
        aid = row[0]
        provider = target.execute("SELECT provider_id FROM album_provider_ids WHERE album_id=? AND provider='youtube' "
                                  'ORDER BY is_primary DESC,provider_id LIMIT 1',(aid,)).fetchone()
        if provider is None:
            raise ValueError('Album has no retained provider representation: '+str(aid))
        counts = reader.execute('SELECT count(*),count(position) FROM album_tracks WHERE album_id=? AND is_full=1',
                                (aid,)).fetchone()
        captured = reader.execute('SELECT * FROM album_fetch_state WHERE album_id=?',(aid,)).fetchone()
        captured = dict(captured) if captured else {}
        tracklist = album_tracklists.begin(target,aid,provider[0],capture_kind='legacy_full',
            order_status='known' if counts[0]==counts[1] else 'unknown' if not counts[1] else 'mixed',
            expected_count=captured.get('expected_track_count'),fetched_at=captured.get('fetched_at'))
        return Batch(dict(phase='rows',album=aid,tracklist=tracklist,last=None,count=0,
                          complete=bool(counts[0] and captured.get('complete') and
                                        captured.get('expected_track_count')==counts[0])),0,False)
    query = 'SELECT * FROM album_tracks WHERE album_id=? AND is_full=1'
    parameters = [cursor['album']]
    if cursor['last'] is not None:
        query += ' AND id>?'
        parameters.append(cursor['last'])
    values = reader.execute(query+' ORDER BY id LIMIT ?',(*parameters,limit)).fetchall()
    for row in values:
        album_tracklists.occurrence(target,cursor['tracklist'],entry_id=row['entry_id'],recording_id=row['recording_id'],
            raw_identity_key=row['legacy_track_key'],position=row['position'],disc_number=row['disc_number'],
            track_number=row['track_number'],captured_source=row['source'],legacy_row_id=row['id'])
    cursor = dict(cursor,count=cursor['count']+len(values),last=values[-1]['id'] if values else cursor['last'])
    if len(values)<limit:
        album_tracklists.finish(target,cursor['tracklist'],complete=cursor['complete'])
        cursor['phase'] = 'next'
    return Batch(cursor,len(values),False)


def _album_observation(reader, target, row):
    if not row['is_full']:
        target.execute('INSERT INTO album_item_observations(album_id,entry_id,provider,recording_id,raw_identity_key,'
            'legacy_row_id,captured_position,captured_disc_number,captured_track_number) VALUES (?,?,?,?,?,?,?,?,?)',
            (row['album_id'],row['entry_id'],row['source'],row['recording_id'],row['legacy_track_key'],row['id'],
             row['position'],row['disc_number'],row['track_number']))


def _share(reader, target, row):
    owner = target.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?",
                           (row['browse_id'],)).fetchone()
    if owner is None:
        raise ValueError('Album share has no retained release: '+row['browse_id'])
    album_tracklists.freeze(target,owner[0],row['browse_id'],row['title'],json.loads(row['video_ids_json']),legacy=True)


def stages():
    from functools import partial
    result = [rows('play_events',partial(_listening,table='play_events',kind='events')),
        rows('listening_observations',partial(_listening,table='listening_observations',kind='observations')),
        rows('rec_recipes',_recipe),rows('cluster_canvas',_canvas),rows('road_trip_drafts',_draft),
        rows('actions',_action),Stage('settings',settings),rows('rec_proposals',proposal),
        rows('album_tracks',_album_observation,name='album_observations'),Stage('album_occurrences',album_tracks),
        rows('album_share_snapshots',_share)]
    for table in discovery.TABLES[:-1]:
        result.append(rows(table,partial(_discovery,table=table)))
    for kind,table in (('track','discovery_memberships'),('album','album_discovery_memberships'),('artist','discovered_artists')):
        result.append(rows(table,partial(_offers,kind=kind),name=kind+'_offers'))
    return tuple(result)
