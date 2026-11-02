"""Released catalog owners imported into their current domain representations."""
from functools import partial
import json

from .. import (artist_credits, genre_identity, legacy_evidence, metadata_legacy,
               metadata_native, recipe_memberships)
from . import development, source
from .build import Batch, Stage


METADATA = frozenset((*metadata_legacy.OBSERVATION_OWNERS, 'recording_field_sources',
    'item_field_sources', 'enrichment_conflict', 'provider_attempts', 'enrichment_provider_state',
    'recording_identifiers', 'catalog_field_hints'))
SCOPES = frozenset({'playlist_scopes','playlist_group','hidden_playlists','overlap_ignored',
    'playlist_provenance','album_library_revisions','album_library_snapshots',
    'album_library_legacy_cohort','album_library_memberships'})
ARCHIVES = frozenset({'legacy_track_evidence','identity_state_archive','legacy_enrichment_attempts'})
CONVERTED = METADATA | SCOPES | ARCHIVES | frozenset({'recordings','albums','track_artists',
    'artist_credit_observations','road_trip_recipes','genre_whitelist','rec_musical_sources'})


def _classified(target, table, row):
    value = dict(row)
    label = value.pop('genre')
    key, reason = genre_identity.resolve(target,label)
    value.update(genre_key=key,unresolved_genre=None if key is not None else label)
    development.insert(target,table,value)
    if label is not None:
        field = {'recordings':'recording_id','albums':'album_id','discovered_artists':'artist_id'}[table]
        target.execute('INSERT INTO genre_classification_legacy('+field+',raw_label,genre_key,resolution_reason) '
                       'VALUES (?,?,?,?)',(row['id'] if table!='discovered_artists' else row['artist_id'],label,key,reason))


def _catalog(reader, target, row, *, table):
    _classified(target,table,row)


def _scope(reader, target, row):
    ref = row['ref']
    value = dict(id=ref,provider_playlist_id=row['provider_id'],account_id=row['identity_id'],kind=row['kind'])
    group = reader.execute('SELECT name FROM playlist_group WHERE ytm=?',(ref,)).fetchone()
    ignored = reader.execute('SELECT created_at FROM overlap_ignored WHERE ytm=?',(ref,)).fetchone()
    provenance = reader.execute('SELECT origin,adopted,description FROM playlist_provenance WHERE ytm=?',(ref,)).fetchone()
    value.update(group_name=group[0] if group else None,
        hidden=int(reader.execute('SELECT 1 FROM hidden_playlists WHERE ytm=?',(ref,)).fetchone() is not None),
        overlap_ignored=int(ignored is not None),overlap_ignored_at=ignored[0] if ignored else None,
        origin=provenance[0] if provenance else None,adopted=provenance[1] if provenance else None,
        provenance_description=provenance[2] if provenance else None)
    development.insert(target,'playlist_scopes',value)


def _account_albums(reader, target, row):
    account = row['id']
    revision = reader.execute('SELECT revision FROM album_library_revisions WHERE identity_id=?',(account,)).fetchone()
    snapshot = reader.execute('SELECT observed_at FROM album_library_snapshots WHERE identity_id=?',(account,)).fetchone()
    cohort = reader.execute('SELECT observed_at FROM album_library_legacy_cohort WHERE identity_id=?',(account,)).fetchone()
    target.execute('INSERT INTO account_album_state VALUES (?,?,?,?,?)',
                   (account,revision[0] if revision else 0,snapshot[0] if snapshot else None,
                    int(cohort is not None),cohort[0] if cohort else None))


def _membership(reader, target, row):
    values = (row['album_id'],row['source'],row['saved_at'])
    if row['identity_id'] is None:
        target.execute('INSERT INTO unattributed_album_memberships VALUES (?,?,?)',values)
    else:
        target.execute('INSERT INTO account_albums VALUES (?,?,?,?)',(row['identity_id'],*values))


def _recipe(reader, target, row):
    value = {k:v for k,v in row.items() if k not in ('artists','genres','blacklist_genres')}
    development.insert(target,'road_trip_recipes',value)
    recipe_memberships.replace(target,row['id'],*(json.loads(row[k]) for k in
        ('artists','genres','blacklist_genres')),legacy=True)


def _credit(reader, target, row):
    raw = row['credits_json']
    if not isinstance(raw, str) or len(raw.encode()) > artist_credits.MAX_BYTES:
        raise ValueError('Artist source credit exceeds its content budget')
    prepared = artist_credits.prepare(target, row['credit'], json.loads(raw), row['source'])
    version = artist_credits.intern(target, prepared)
    target.execute('INSERT INTO artist_credit_observations('
        'id,entry_id,credit_id,source,first_seen,last_seen,processing_count,capture_kind) '
        "VALUES (?,?,?,?,?,?,1,'legacy_capture')",
        (row['id'],row['entry_id'],version,row['source'],row['observed_at'],row['observed_at']))


def _selected_credits(reader, target, cursor, limit):
    entries = reader.execute('SELECT DISTINCT entry_id FROM track_artists WHERE entry_id>? '
                             'ORDER BY entry_id LIMIT ?',(cursor if cursor is not None else -1,limit)).fetchall()
    for entry, in entries:
        rows = reader.execute('SELECT artist_id,credit,source,position FROM track_artists '
                              'WHERE entry_id=? ORDER BY position LIMIT 129',(entry,)).fetchall()
        if len(rows)>128 or [row['position'] for row in rows]!=list(range(len(rows))):
            raise ValueError('Invalid released artist-credit membership: '+str(entry))
        display = reader.execute('SELECT artist FROM catalog_tracks WHERE id=?',(entry,)).fetchone()[0] or ''
        prepared = artist_credits.Credit(display,tuple(artist_credits.Member(r['artist_id'],r['credit']) for r in rows))
        version = artist_credits.intern(target,prepared)
        labels = {row['source'] for row in rows}
        label = next(iter(labels)) if len(labels)==1 else 'legacy_selected_sources'
        observation = target.execute('INSERT INTO artist_credit_observations('
            "entry_id,credit_id,source,processing_count,capture_kind) VALUES (?,?,?,1,'selection_snapshot')",
            (entry,version,label)).lastrowid
        target.execute('INSERT INTO entry_artist_selections VALUES (?,?)',(entry,observation))
        target.executemany('INSERT INTO entry_artists VALUES (?,?,?,?,?)',
                           [(entry,r['position'],r['artist_id'],r['credit'],r['source']) for r in rows])
    return Batch(entries[-1][0] if entries else cursor,len(entries),len(entries)<limit)


def _archive(reader, target, row, *, table):
    if table=='legacy_track_evidence':
        domain,kind,key,reason = 'catalog',row['kind'],row['source_id'],'retained original catalog capture'
    elif table=='legacy_enrichment_attempts':
        domain,kind,key,reason = 'metadata','legacy_enrichment_attempt',row['identity_key'],row['reason']
    else:
        kind,key,reason = row['kind'],row['source_id'],row['reason']
        domain = legacy_evidence.archive_domain(kind)
    legacy_evidence.retain_many(target,domain,[(kind,key,row['payload'],reason)])
    if kind=='ambiguous_album_artist':
        artist = target.execute('SELECT id FROM artists WHERE CAST(id AS TEXT)=?',(key,)).fetchone()
        if artist:
            legacy_evidence.mark_artist_ambiguity(target,artist[0])


def _metadata(reader, target, cursor, limit, *, table):
    rows,bookmark,done = source.scan(reader,table,cursor,limit,legacy_id=True)
    if table in ('provider_attempts','enrichment_provider_state','recording_identifiers'):
        fields = set(source.columns(target,table))
        for row in rows:
            development.insert(target,table,{k:v for k,v in row.items() if k in fields})
    if table in metadata_legacy.OBSERVATION_OWNERS:
        metadata_legacy.copy_observations(target,table,rows)
    elif table in ('recording_field_sources','item_field_sources'):
        metadata_legacy.copy_selections(target,table,rows)
    elif table=='enrichment_conflict':
        metadata_legacy.backfill_conflicts(target,batches=[rows])
    elif table in ('provider_attempts','enrichment_provider_state'):
        metadata_legacy.backfill_operational(target,sources={table:[rows]})
    elif table=='recording_identifiers':
        metadata_legacy.backfill_identifiers(target,batches=[rows])
    elif table=='catalog_field_hints':
        metadata_legacy.backfill_hints(target,batches=[rows])
    return Batch(bookmark,len(rows),done)


def _musical_source(reader, target, row):
    development.insert(target,'rec_musical_sources',{k:v for k,v in row.items() if k!='payload'})


def verify(reader, target):
    from .verification import rows
    for table in ('recordings','albums','road_trip_recipes','rec_musical_sources'):
        omitted = {'genre','artists','genres','blacklist_genres','payload'}
        for row in rows(reader,table):
            fields = [key for key in row if key not in omitted]
            key = 'source_key' if table=='rec_musical_sources' else 'id'
            actual = target.execute('SELECT '+','.join(fields)+' FROM '+table+' WHERE '+key+'=?',(row[key],)).fetchone()
            if actual is None or tuple(actual)!=tuple(row[field] for field in fields):
                raise ValueError('Released catalog values changed: '+table+'/'+str(row[key]))
            if table in ('recordings','albums') and row['genre'] is not None:
                subject = 'recording_id' if table=='recordings' else 'album_id'
                captured = target.execute('SELECT raw_label FROM genre_classification_legacy WHERE '+subject+'=?',
                                           (row['id'],)).fetchone()
                if captured is None or captured[0]!=row['genre']:
                    raise ValueError('Released genre attribution changed: '+table)
    for table in (*metadata_legacy.OBSERVATION_OWNERS,'enrichment_conflict','provider_attempts',
                  'enrichment_provider_state','recording_identifiers','catalog_field_hints'):
        expected = reader.execute('SELECT count(*) FROM '+table).fetchone()[0]
        actual = target.execute('SELECT count(*) FROM metadata_legacy_records WHERE legacy_owner=?',(table,)).fetchone()[0]
        if expected!=actual:
            raise ValueError('Released metadata capture is incomplete: '+table)
    for key,raw in reader.execute('SELECT source_key,payload FROM rec_musical_sources'):
        if metadata_native.source_values(target,key)!=json.loads(raw):
            raise ValueError('Released native metadata projection changed: '+key)


def stages(*, sparse=False):
    yield from (development.rows(t,partial(_catalog,table=t)) for t in ('recordings','albums'))
    yield development.rows('playlist_scopes',_scope)
    yield development.rows('identities',_account_albums,name='account_album_state')
    yield development.rows('album_library_memberships',_membership)
    yield development.rows('road_trip_recipes',_recipe)
    if not sparse:
        yield development.rows('artist_credit_observations',_credit)
    yield Stage('selected_artist_credits',_selected_credits)
    yield development.rows('rec_musical_sources',_musical_source)
    for table in sorted(ARCHIVES if not sparse else ()):
        yield development.rows(table,partial(_archive,table=table))
    for table in sorted(METADATA):
        if sparse and table not in ('recording_field_sources','item_field_sources',
                                    'metadata_decisions','entry_field_choices','recording_identifiers'):
            continue
        yield Stage('metadata:'+table,partial(_metadata,table=table))
