"""Bounded imports of released learned state and captured model inputs."""
from functools import partial
import hashlib
import json
import math
import struct

from .. import genre_identity, taste_axes
from ..migrations import (v066_explicit_keys as mood, v067_audio_annotations as audio,
                          v068_canonical_embeddings as embeddings, v069_prepared_recording_inputs as prepared,
                          v088_taste_axes as axes)
from ...util import genre_catalog
from . import development, source
from .build import Batch, Stage


CONVERTED = frozenset((*axes.OLD_TABLES,*embeddings.SOURCES,'rec_mood','rec_prepared_content',
                      'audio_vibe_feedback','audio_vibe_labels','audio_session_labels','genre_whitelist'))
VECTOR_MAP = '_database_rebuild_embedding_owners'


def _axis(reader, target, row, *, table):
    token = row['facet' if table=='rec_theme' else 'axis']
    axis = taste_axes.identity(target,token,create=True)
    target.execute('INSERT INTO taste_axis_legacy(table_name,row_key,raw_axis,axis_id,weight,lean_value,score,'
                   'updated_at,last_graduated_day,source,factor,new_weight,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (table,str(row.get('id',token)),token,axis,*[row.get(k) for k in
        ('weight','value','score','updated_at','last_graduated_day','source','factor','new_weight','created_at')]))
    value = {k:v for k,v in row.items() if k not in ('axis','facet')}
    value['axis_id'] = axis
    destination = axes.RENAMES.get(table,table)
    if table=='home_hidden_facet':
        target.execute('INSERT OR IGNORE INTO home_hidden_facet VALUES (?)',(axis,))
    elif table=='rec_theme':
        target.execute('INSERT INTO rec_theme VALUES (?,?,?) ON CONFLICT(axis_id) DO UPDATE SET '
                       'score=score+excluded.score,updated_at=max(updated_at,excluded.updated_at)',
                       (axis,row['score'],row['updated_at']))
    elif table=='rec_play_grad':
        target.execute('INSERT INTO play_gradient_checkpoints VALUES (?,?) ON CONFLICT(axis_id) DO UPDATE SET '
            'last_graduated_day=CASE WHEN last_graduated_day IS NULL THEN excluded.last_graduated_day '
            'WHEN excluded.last_graduated_day IS NULL THEN last_graduated_day '
            'ELSE max(last_graduated_day,excluded.last_graduated_day) END',(axis,row['last_graduated_day']))
    else:
        # Competing configured values deliberately fail their unique owner key.
        development.insert(target,destination,value)


def _mood(reader, target, cursor, limit):
    rows,bookmark,done = source.scan(reader,'rec_mood',cursor,limit,legacy_id=True)
    for row in rows:
        keys = json.loads(row['keys'])
        if not isinstance(keys,list) or any(not isinstance(key,str) for key in keys):
            raise ValueError('Invalid original mood seeds')
        event = row['_legacy_id']
        target.execute('INSERT INTO mood_events VALUES (?,?,?)',(event,row['created_at'],row['direction']))
        seen = set()
        for position,key in enumerate(keys):
            owner,reason = mood._resolve(target,key)
            identity = ('recording',owner) if owner is not None else ('unresolved',key)
            if identity in seen:
                continue
            seen.add(identity)
            if owner is None:
                target.execute('INSERT INTO mood_event_unresolved_seeds VALUES (?,?,?,?)',(event,key,position,reason))
            else:
                target.execute('INSERT INTO mood_event_recordings VALUES (?,?,?)',(event,owner,position))
    return Batch(bookmark,len(rows),done)


def _input(reader, target, row):
    key,raw,digest,captured,revision = (row[k] for k in ('identity_key','payload','digest','captured_at','revision'))
    values,empty = prepared._flatten(raw)
    if hashlib.sha256(raw.encode()).hexdigest()!=digest or type(revision) is not int or revision<0:
        raise ValueError('Invalid original prepared-input identity: '+key)
    owner = target.execute('SELECT r.id,d.retired_id FROM recordings r LEFT JOIN recording_redirects d '
                            'ON d.retired_id=r.id WHERE r.identity_key=?',(key,)).fetchone()
    value = dict(zip(prepared.FIELDS,values),input_revision=revision,digest=digest,built_at=captured)
    if owner and owner[1] is None:
        development.insert(target,'prepared_recording_inputs',dict(recording_id=owner[0],**value))
    else:
        reason = 'empty_retraction' if empty else 'retired_owner' if owner else 'missing_owner'
        development.insert(target,'unresolved_preparation_inputs',dict(raw_key=key,reason=reason,**value))


def _vectors(reader, target, cursor, limit, *, table):
    kind,space,column = embeddings.SOURCES[table]
    if cursor is None:
        target.execute('CREATE TABLE IF NOT EXISTS '+VECTOR_MAP+'('
            'source TEXT NOT NULL,raw_key TEXT NOT NULL,subject INTEGER,reason TEXT,'
            'PRIMARY KEY(source,raw_key)) WITHOUT ROWID')
        target.execute('CREATE INDEX IF NOT EXISTS '+VECTOR_MAP+'_subject ON '+VECTOR_MAP+'(source,subject)')
        settings = source.settings(reader,{'rec_content_model','rec_artist_content_model','rec_fit_inputs',
            'rec_artist_fit_inputs','rec_embed_method','rec_dim'})
        basis = embeddings._basis(reader,table,kind,space,settings)
        return Batch(dict(phase='owners',last=None,basis=basis,dimensions=None),0,False)
    rows,bookmark,done = source.scan(reader,table,cursor['last'],limit)
    dimensions = cursor['dimensions']
    for row in rows:
        key,vector = row[column],row['vec']
        if cursor['phase']=='owners':
            if (not isinstance(vector,bytes) or not vector or len(vector)%4
                    or any(not math.isfinite(v) for v, in struct.iter_unpack('=f',vector))):
                raise ValueError('Invalid original float32 embedding: '+table+'/'+str(key))
            dim = len(vector)//4
            if dimensions is not None and dimensions!=dim:
                raise ValueError('Mixed original embedding dimensions: '+table)
            dimensions = dim
            model = source.settings(reader,{'rec_content_model' if kind=='recording' else 'rec_artist_content_model'})
            raw = next(iter(model.values()),None)
            if space.startswith('content') and raw:
                encoder = json.loads(raw)
                if dim!=encoder['ncat']+len(encoder['cont']):
                    raise ValueError('Original embedding differs from its encoder: '+table)
            owner,reason = embeddings._owner(target,kind,key)
            target.execute('INSERT INTO '+VECTOR_MAP+' VALUES (?,?,?,?)',(table,key,owner,reason))
        else:
            owner,reason = target.execute('SELECT subject,reason FROM '+VECTOR_MAP+' WHERE source=? AND raw_key=?',
                                          (table,key)).fetchone()
            if reason is None and target.execute('SELECT 1 FROM '+VECTOR_MAP+
                    ' WHERE source=? AND subject=? AND raw_key<>? LIMIT 1',(table,owner,key)).fetchone():
                reason = 'ambiguous_owner'
            if reason:
                target.execute('INSERT INTO unresolved_legacy_embeddings VALUES (?,?,?,?,?,NULL,?)',
                               (table,key,cursor['basis'],len(vector)//4,vector,reason))
            else:
                target.execute('INSERT INTO '+kind+'_embeddings VALUES (?,?,?,?,?,NULL)',
                               (owner,space,cursor['basis'],len(vector)//4,vector))
    if done and cursor['phase']=='owners':
        return Batch(dict(phase='vectors',last=None,basis=cursor['basis'],dimensions=dimensions),len(rows),False)
    if done:
        target.execute('DELETE FROM '+VECTOR_MAP+' WHERE source=?',(table,))
    return Batch(dict(cursor,last=bookmark,dimensions=dimensions),len(rows),done)


def _annotation(reader, target, row, *, table, kind):
    audio.copy_row(target,audio.prepare_row(table,kind,row))


def preferences(reader, target, cursor, limit):
    if cursor is None:
        settings = source.settings(reader,{'genres_seeded','genre_defaults'})
        baseline = json.loads(settings['genre_defaults']) if 'genre_defaults' in settings else genre_catalog.legacy_names()
        if not isinstance(baseline,list) or any(not isinstance(label,str) for label in baseline):
            raise ValueError('Invalid original genre defaults')
        if settings.get('genres_seeded') or reader.execute('SELECT 1 FROM genre_whitelist LIMIT 1').fetchone():
            for label in baseline:
                key,reason = genre_identity.resolve(target,label)
                if key is not None:
                    target.execute("INSERT OR IGNORE INTO genre_preferences VALUES (?,0,'default')",(key,))
        return Batch(['enabled',None],0,False)
    rows,bookmark,done = source.scan(reader,'genre_whitelist',cursor[1],limit)
    for row in rows:
        key = genre_identity.add_custom(target,row['name'])
        target.execute("INSERT INTO genre_preferences VALUES (?,1,'legacy') ON CONFLICT(genre_key) "
                       "DO UPDATE SET enabled=1,origin='legacy'",(key,))
    return Batch(['enabled',bookmark],len(rows),done)


def finish(reader, target, cursor, limit):
    target.execute('DROP TABLE IF EXISTS '+VECTOR_MAP)
    archived = reader.execute("SELECT max(CAST(source_id AS INTEGER)) FROM identity_state_archive "
        "WHERE kind='rec_mood' AND source_id=CAST(CAST(source_id AS INTEGER) AS TEXT)").fetchone()[0]
    if archived is not None:
        target.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 'mood_events',? "
                       "WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='mood_events')",(archived,))
        target.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='mood_events'",(archived,))
    return Batch(done=True)


def stages():
    for table in axes.OLD_TABLES:
        yield development.rows(table,partial(_axis,table=table))
    yield Stage('mood_events',_mood)
    yield development.rows('rec_prepared_content',_input)
    for table in embeddings.SOURCES:
        yield Stage('embedding:'+table,partial(_vectors,table=table))
    for table,kind in (('audio_vibe_feedback','feedback'),('audio_vibe_labels','development'),('audio_session_labels','session')):
        yield development.rows(table,partial(_annotation,table=table,kind=kind))
    yield Stage('released_learning_cleanup',finish)
