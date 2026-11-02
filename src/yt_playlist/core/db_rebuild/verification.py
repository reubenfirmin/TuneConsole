"""Preservation checks independent of the importer callbacks and row counts."""
import json
from itertools import zip_longest

from .. import collection_recovery, radio_storage
from ..preparation.listening_subjects import describe
from . import source
from .build import quote


def rows(conn, table, *, legacy_id=False, predicate=None):
    cursor = None
    while True:
        values,cursor,done = source.scan(conn,table,cursor,128,legacy_id=legacy_id,predicate=predicate)
        yield from values
        if done:
            return


def predictions(reader, target):
    """Check converted lineage, including roots added from original SQL edges."""
    from .. import vibe_provenance
    tables = source.tables(reader)
    expected = 'SELECT measurement_id FROM prediction_inputs'
    if 'prediction_measurement_roots' in tables:
        expected += ' UNION SELECT measurement_id FROM prediction_measurement_roots'
    else:
        expected = 'SELECT DISTINCT measurement_id FROM prediction_inputs'
    for original, actual in zip_longest(reader.execute(expected+' ORDER BY measurement_id'),
            target.execute('SELECT measurement_id FROM prediction_measurement_roots ORDER BY measurement_id')):
        if (tuple(original) if original else None) != (tuple(actual) if actual else None):
            raise ValueError('Direct rebuild changed original prediction measurement roots')
    expected_count = reader.execute('SELECT count(*) FROM predictions').fetchone()[0]
    if target.execute('SELECT count(*) FROM prediction_provenance').fetchone()[0] != expected_count:
        raise ValueError('Direct rebuild changed calculation provenance count')
    original = vibe_provenance.read_many(reader, (row[0] for row in reader.execute('SELECT id FROM predictions ORDER BY id')))
    actual = vibe_provenance.read_many(target, (row[0] for row in target.execute('SELECT id FROM predictions ORDER BY id')))
    for expected, found in zip_longest(original, actual):
        if expected != found:
            raise ValueError('Direct rebuild changed calculation provenance: '+str((expected or found)[0]))


def retained(reader, target, table, destination, mapping, *, retained=None, at=None, required=None,
             predicate=None, nulls=()):
    """Every original key/value survives, including implicit row IDs and types.

    Domain writers may add exact upload admissions and vocabulary identities.
    Those owners are checked separately; all other direct copies are bijective.
    """
    from .. import group_documents, legacy_evidence, legacy_journals, policy_documents, policy_history
    if table == 'group_preparation_progress':
        _group_progress(reader, target)
        return
    if table in ('legacy_archive_heads','policy_history_archive'):
        from .. import file_map
        for row in rows(reader,table):
            if table == 'legacy_archive_heads':
                domain = row['domain']
                original = legacy_journals.directory(reader) / domain
                rebuilt = legacy_journals.directory(target) / domain
                publication = legacy_journals.head(target,domain)
            else:
                original, rebuilt = policy_history.directory(reader), policy_history.directory(target)
                publication = policy_history.head(target)
            for key, expected in file_map.items(original,row['publication'].hex()):
                if file_map.get(rebuilt,publication,key) != expected:
                    raise ValueError('Direct rebuild changed retained archive contents: '+table)
        return
    if table=='recommendation_policies':
        pending = set()
        for row in rows(reader,table):
            if retained is not None and row['version'] not in retained:
                continue
            active = target.execute('SELECT 1 FROM recommendation_policies WHERE version=?',(row['version'],)).fetchone()
            if not active:
                pending.add(row['version'])
                continue
            expected = policy_documents.configuration(reader,row['version'])
            actual = policy_documents.configuration(target,row['version'])
            if actual!=expected:
                raise ValueError('Direct rebuild changed retained policy configuration: '+row['version'])
        for version,actual in policy_history.configurations(target):
            if version in pending:
                if actual!=policy_documents.configuration(reader,version):
                    raise ValueError('Direct rebuild changed retained policy configuration: '+version)
                pending.remove(version)
        if pending:
            raise ValueError('Direct rebuild lost retained policy configuration: '+min(pending))
        return
    if table=='recommendation_impression_policies':
        for row in rows(reader,table):
            actual = target.execute('SELECT * FROM recommendation_impression_policies WHERE impression_id=? AND kind=?',
                                    (row['impression_id'],row['kind'])).fetchone()
            if actual is not None and dict(actual)==row:
                continue
            historical = target.execute('SELECT policy_version FROM historical_impression_policies WHERE impression_id=? AND kind=?',
                                         (row['impression_id'],row['kind'])).fetchone()
            if historical is None or historical[0]!=row['policy_version'] or row['resolution_reason'] is not None:
                raise ValueError('Direct rebuild changed impression attribution')
        return
    if table in legacy_evidence.DOMAINS.values():
        domain = next(name for name,owner in legacy_evidence.DOMAINS.items() if owner==table)
        for row in rows(reader,table):
            original = legacy_evidence._read(domain,row['filename'],row['content_digest'],row['byte_size'],row['file_offset'])[1]
            actual = legacy_journals.read(target,domain,row['kind'],row['source_id'])
            if (actual is None or actual['payload']!=original or actual['reason']!=row['reason']
                    or actual['archived_at']!=row['archived_at']
                    or actual.get('resolution_status')!=row['resolution_status']
                    or actual['subjects']!={name:row[name] for name in legacy_evidence.SUBJECTS[domain]}):
                raise ValueError('Direct rebuild changed original archive meaning: '+table)
        if target.execute('SELECT 1 FROM '+quote(destination)+' LIMIT 1').fetchone():
            raise ValueError('Direct rebuild retained redundant archive receipts: '+table)
        return
    if table in ('recording_embeddings','artist_embeddings'):
        from itertools import islice
        from .. import embedding_files
        kind = table.removesuffix('_embeddings')
        logical = embedding_files.relation(target,kind)
        originals = iter(rows(reader,table))
        count = 0
        while batch := list(islice(originals,128)):
            heads = []
            for row in batch:
                actual = target.execute(f'SELECT * FROM {logical} WHERE {kind}_id=? AND space=? AND model_version=?',
                    (row[kind+'_id'],row['space'],row['model_version'])).fetchone()
                fields = (kind+'_id','space','model_version','dimensions','built_at')
                if actual is None or any(actual[field] != row[field] for field in fields):
                    raise ValueError('Direct rebuild changed embedding ownership: '+table)
                heads.append((row[kind+'_id'],actual['vector'],actual['dimensions'],actual['publication'],actual['position']))
            if [vector for _,vector in embedding_files.load_many(target,kind,heads)] != [row['vector'] for row in batch]:
                raise ValueError('Direct rebuild changed exact embedding bytes: '+table)
            count += len(batch)
        if target.execute('SELECT count(*) FROM '+logical).fetchone()[0] != count:
            raise ValueError('Direct rebuild changed embedding count: '+table)
        return
    from .. import vibe_versions
    transformed = {'artist_credit_observations','taste_modes','discovery_memberships',
                   'album_discovery_memberships','rec_musical_profiles','rec_musical_source_profiles',
                   *vibe_versions.COLUMNS,*(owner for owner,*_ in group_documents.OWNERS)}
    attached = any(row[1]=='rebuild_source' for row in target.execute('PRAGMA database_list'))
    if table not in transformed and retained is None and required is None and predicate is None and not nulls and attached:
        _exact_sql(reader,target,table,destination,mapping)
        return
    info = list(reader.execute('PRAGMA table_info('+quote(table)+')'))
    keys = [r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]] or ['_legacy_id']
    fields = ','.join(map(quote,mapping.values()))
    query = 'SELECT '+fields+' FROM '+quote(destination)+' WHERE '+ ' AND '.join(
        quote(mapping[key])+' IS ?' for key in keys)
    count, profile_cache = 0, {}
    for row in rows(reader,table,legacy_id='_legacy_id' in mapping,predicate=predicate):
        if retained is not None and row['version'] not in retained:
            continue
        if table == 'artist_credit_observations' and at is not None:
            from .runtime_history import retired_credit
            if retired_credit(reader, row, at):
                if target.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (row['id'],)).fetchone():
                    raise ValueError('Rebuild retained an obsolete artist capture')
                continue
        actual = target.execute(query,tuple(row[key] for key in keys)).fetchone()
        if actual is None and required is not None and not required(row):
            continue
        expected_row = (_membership(target,table,row) if table in (
            'discovery_memberships','album_discovery_memberships') else row)
        if table == 'taste_modes' and at is not None and not row['active']:
            expected_row = dict(row, centroid=b'', families='[]', rep_keys='[]')
        expected = tuple(None if key in nulls else expected_row[key] for key in mapping)
        group_owner = next(((prefix, kind) for owner, _keys, prefix, kind in group_documents.OWNERS if owner == table), None)
        if actual is not None and group_owner is not None:
            actual = list(actual)
            prefix, kind = group_owner
            fields = [prefix + suffix for suffix in ('_filename', '_digest', '_bytes', '_offset')]
            reference = [actual[list(mapping).index(field)] for field in fields]
            original = [row[field] for field in fields]
            if group_documents.read(*reference, kind) != group_documents.read(*original, kind):
                raise ValueError('Direct rebuild changed retained group document: ' + table)
            # Exact digests and byte sizes still compare below; physical pack
            # location is independently checked by the domain reader above.
            for field in (prefix + '_filename', prefix + '_offset'):
                actual[list(mapping).index(field)] = row[field]
        from .. import vibe_versions
        if actual is not None and table in vibe_versions.COLUMNS:
            actual = list(actual)
            index = list(mapping).index(vibe_versions.COLUMNS[table])
            value = target.execute('SELECT value FROM vibe_versions WHERE id=?', (actual[index],)).fetchone()
            actual[index] = value[0] if value else None
        if actual is not None and table in ('rec_musical_profiles','rec_musical_source_profiles'):
            from .. import profile_files
            actual = list(actual)
            kind = 'recording' if table=='rec_musical_profiles' else 'source'
            key = 'identity_key' if kind=='recording' else 'source_key'
            actual[list(mapping).index('payload')] = profile_files.read(target,kind,row[key],cache=profile_cache)
        if actual is None or tuple(actual)!=expected or tuple(map(type,actual))!=tuple(map(type,expected)):
            raise ValueError('Direct rebuild changed retained values or IDs: '+table)
        count += 1
    actual = target.execute('SELECT count(*) FROM '+quote(destination)).fetchone()[0]
    may_admit = table in ('genre_family','genre_catalog','genre_alias','recordings',
                         'catalog_entries','youtube_items','playlist_scopes','taste_axes','vibe_versions',
                         'recording_profile_files','source_profile_files','recording_embedding_files',
                         'artist_embedding_files','recommendation_policy_history','historical_impression_policies')
    from . import mix_history
    may_admit = may_admit or table in mix_history.DICTIONARY
    if actual<count or actual!=count and not may_admit:
        raise ValueError('Direct rebuild changed retained row count: '+table)


def _exact_sql(reader, target, table, destination, mapping):
    """Check unchanged owners by key/value/type joins, without Python per row.

    BINARY comparisons preserve exact text even on a NOCASE source identity.
    The separate type check distinguishes an integer from an equal real value.
    """
    def original(name):
        return 's.'+quote(source.keys(reader,table)[0] if name=='_legacy_id' else name)
    info = list(reader.execute('PRAGMA table_info('+quote(table)+')'))
    keys = (['_legacy_id'] if '_legacy_id' in mapping else
            [row[1] for row in sorted(info,key=lambda row:row[5]) if row[5]])
    join = ' AND '.join(original(key)+' IS t.'+quote(mapping[key])+' COLLATE BINARY' for key in keys)
    equal = ' AND '.join('('+original(name)+' IS t.'+quote(field)+' COLLATE BINARY AND typeof('
        +original(name)+')=typeof(t.'+quote(field)+'))' for name,field in mapping.items())
    changed = target.execute('SELECT 1 FROM rebuild_source.'+quote(table)+' s LEFT JOIN main.'
        +quote(destination)+' t ON '+join+' WHERE NOT ('+equal+') LIMIT 1').fetchone()
    if changed:
        raise ValueError('Direct rebuild changed retained values or IDs: '+table)
    expected = reader.execute('SELECT count(*) FROM '+quote(table)).fetchone()[0]
    actual = target.execute('SELECT count(*) FROM '+quote(destination)).fetchone()[0]
    from . import mix_history
    may_admit = table in {'genre_family','genre_catalog','genre_alias','recordings','catalog_entries',
        'youtube_items','playlist_scopes','taste_axes','vibe_versions','recording_profile_files',
        'source_profile_files','recording_embedding_files','artist_embedding_files',
        'recommendation_policy_history','historical_impression_policies',*mix_history.DICTIONARY}
    if actual<expected or actual!=expected and not may_admit:
        raise ValueError('Direct rebuild changed retained row count: '+table)


def _membership(target, table, original):
    """Offers become legacy evidence; only captured visibility counts as seen."""
    import hashlib
    from .. import legacy_evidence
    column = 'recording_id' if table == 'discovery_memberships' else 'album_id'
    # Physical row IDs are compared by the direct copier, not part of the
    # captured domain input sent to the discovery conversion.
    key = json.dumps({k:v for k,v in original.items() if k != '_legacy_id'},sort_keys=True,separators=(',',':'))
    captured = legacy_evidence.original(target,'discovery','discovery_cutover:'+table,hashlib.sha256(key.encode()).hexdigest())
    if captured != key:
        raise ValueError('Direct rebuild lost original discovery counters: '+table)
    count,first,last = target.execute('SELECT count(*),min(shown_at),max(shown_at) '
        'FROM discovery_impressions WHERE '+column+'=?',(original[column],)).fetchone()
    expected = dict(original,offered_count=count,first_shown=first,last_shown=last)
    unverified = max(0,(original['offered_count'] or 0)-count)
    starts = [v for v in (original.get('legacy_first_shown'),original['first_shown'] if unverified else None) if v is not None]
    ends = [v for v in (original.get('legacy_last_shown'),original['last_shown'] if unverified else None) if v is not None]
    legacy = (original.get('legacy_offered_count',0)+unverified,
              min(starts) if starts else None,max(ends) if ends else None)
    saved = target.execute('SELECT offered_count,first_shown,last_shown FROM discovery_legacy_offers '
        'WHERE '+column+'=?',(original[column],)).fetchone()
    if (tuple(saved) if saved is not None else (0,None,None)) != legacy:
        raise ValueError('Direct rebuild changed legacy discovery offers: '+table)
    if column == 'album_id':
        expected.update(zip(('legacy_offered_count','legacy_first_shown','legacy_last_shown'),legacy))
    return expected


def _capture(row, kind, *, owner=None):
    if row is None:
        raise ValueError('Missing converted collection owner: '+kind)
    return collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind=kind,owner=row['id'] if owner is None else owner)


def _group_progress(reader, target):
    """The empty additive owner may gain the captured legacy progress row."""
    expected = ([tuple(row) for row in reader.execute(
        'SELECT singleton,phase,reported_at,reason FROM group_preparation_progress')]
        if 'group_preparation_progress' in source.tables(reader) else [])
    setting = reader.execute("SELECT value FROM settings WHERE key='rec_group_shadow_progress'").fetchone()
    if setting is not None:
        value = json.loads(setting[0]) if setting[0] else {}
        captured = [(1, value.get('phase'), value.get('at'), value.get('reason'))]
        if expected and expected != captured:
            raise ValueError('Direct rebuild found conflicting original grouping progress')
        expected = captured
    actual = [tuple(row) for row in target.execute(
        'SELECT singleton,phase,reported_at,reason FROM group_preparation_progress')]
    if not expected and actual == [(1,None,None,None)]:
        # Importing a captured generation checkpoint can create the empty
        # progress singleton. It asserts no preparation phase or clock.
        actual = []
    if actual != expected:
        raise ValueError('Direct rebuild changed captured grouping progress')


def converted(reader, target):
    _group_progress(reader, target)
    for table,kind in (('play_events','events'),('listening_observations','observations')):
        for row in rows(reader,table):
            expected = describe(reader,kind,row)
            expected.pop('source_snapshot')
            actual = target.execute('SELECT '+','.join(map(quote,expected))+' FROM '+table+' WHERE id=?',(row['id'],)).fetchone()
            if actual is None or tuple(actual)!=tuple(expected.values()):
                raise ValueError('Direct rebuild changed captured listening evidence: '+str(row['id']))
    for row in rows(reader,'album_tracks'):
        if row['is_full']:
            actual = target.execute('SELECT t.album_id,o.recording_id,o.raw_identity_key,o.position,'
                'o.disc_number,o.track_number,o.captured_source,o.id FROM album_track_occurrences o '
                'JOIN album_tracklists t ON t.id=o.tracklist_id WHERE o.legacy_row_id=?',(row['id'],)).fetchone()
            recording = reader.execute('SELECT recording_id FROM catalog_entries WHERE id=?',
                                       (row['entry_id'],)).fetchone()
            expected = (row['album_id'],recording[0] if recording else row['recording_id'],row['legacy_track_key'],
                        row['position'],row['disc_number'],row['track_number'],row['source'])
            if actual is None or tuple(actual)[:-1]!=expected:
                raise ValueError('Direct rebuild changed an original album occurrence')
            entries = [r[0] for r in target.execute('SELECT entry_id FROM album_occurrence_sources WHERE occurrence_id=?',
                                                   (actual[-1],))]
            if entries!=([row['entry_id']] if row['entry_id'] is not None else []):
                raise ValueError('Direct rebuild changed a captured album source')
        else:
            actual = target.execute('SELECT album_id,entry_id,recording_id,raw_identity_key,captured_position,'
                'captured_disc_number,captured_track_number,provider FROM album_item_observations WHERE legacy_row_id=?',
                (row['id'],)).fetchone()
            expected = tuple(row[k] for k in ('album_id','entry_id','recording_id','legacy_track_key',
                                              'position','disc_number','track_number','source'))
            if actual is None or tuple(actual)!=expected:
                raise ValueError('Direct rebuild changed an incidental album observation')
    for row in rows(reader,'actions'):
        actual = target.execute('SELECT * FROM actions WHERE id=?',(row['id'],)).fetchone()
        captured = _capture(actual,'action')
        if captured!={'params':row['params_json'],'plan':row['plan_json'],'undo':row['undo_json']} or any(
                actual[key]!=row[key] for key in ('id','kind','created_at','executed_at')):
            raise ValueError('Direct rebuild changed an original action: '+str(row['id']))
    for row in rows(reader,'rec_recipes'):
        actual = target.execute('SELECT * FROM rec_recipes WHERE playlist_scope_id=?',(row['playlist_ytm'],)).fetchone()
        if _capture(actual,'playlist-recipe')!=json.loads(row['recipe']) or actual['created_at']!=row['created_at']:
            raise ValueError('Direct rebuild changed a saved playlist recipe')
    for row in rows(reader,'cluster_canvas'):
        actual = target.execute('SELECT * FROM cluster_canvas WHERE playlist_scope_id=?',(row['playlist_ytm'],)).fetchone()
        if _capture(actual,'playlist-canvas')!=row['state'] or actual['created_at']!=row['created_at']:
            raise ValueError('Direct rebuild changed a saved playlist canvas')
    admitted = set()
    for row in rows(reader,'road_trip_drafts'):
        original = json.loads(row['state'])
        actual = target.execute('SELECT * FROM road_trip_drafts WHERE recipe_id=?',(row['recipe_id'],)).fetchone()
        captured = _capture(actual,'road-trip-draft')
        if not original.get('revision'):
            if not captured.get('revision','').startswith('legacy:'):
                raise ValueError('Missing explicit legacy draft revision')
            captured.pop('revision')
        expected = {k:v for k,v in original.items() if k not in ('picks','banned')}
        if not original.get('revision'):
            expected.pop('revision',None)
        if captured!=expected or actual['account_id'] is not None or not actual['legacy_author_unknown']:
            raise ValueError('Direct rebuild changed a draft decision or invented its author')
        for state,key in (('picked','picks'),('excluded','banned')):
            videos = original.get(key,[])
            selected = target.execute('SELECT video_id FROM road_trip_draft_entries '
                'WHERE draft_id=? AND selection_state=? ORDER BY position',(actual['id'],state)).fetchall()
            if [r[0] for r in selected]!=videos:
                raise ValueError('Direct rebuild changed ordered draft selections')
            admitted.update(videos)
    row = reader.execute("SELECT value FROM settings WHERE key='radio_current_session'").fetchone()
    if row and row[0] and json.loads(row[0]) is not None:
        original = json.loads(row[0])
        captured,_ = radio_storage.load_published(target)
        if not original.get('episode_id'):
            if not captured.get('episode_id','').startswith('legacy:'):
                raise ValueError('Missing explicit legacy radio episode')
            captured.pop('episode_id')
            original.pop('episode_id',None)
        if captured!=original:
            raise ValueError('Direct rebuild changed a radio checkpoint')
        tracks = [*original.get('tracks',[]),*original.get('initial_tracks',[])]
        if original.get('pending'):
            tracks.append(original['pending'])
        admitted.update(track['video_id'] for track in tracks)
        selected = target.execute('SELECT q.video_id FROM radio_session_head h JOIN radio_queue_items q '
            'ON q.session_id=h.session_id ORDER BY q.position').fetchall()
        if [r[0] for r in selected]!=[track['video_id'] for track in tracks]:
            raise ValueError('Direct rebuild changed the ordered radio queue')
    # New catalog subjects require an exact upload recorded in a durable input.
    for entry,video,recording,key in target.execute('SELECT e.id,e.video_id,e.recording_id,r.identity_key '
            'FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id'):
        if reader.execute('SELECT 1 FROM catalog_entries WHERE id=?',(entry,)).fetchone():
            continue
        if video not in admitted or key!='@unresolved-video:'+video:
            raise ValueError('Direct rebuild admitted an unrecorded catalog subject')
    for table,key in (('recordings','id'),('youtube_items','video_id')):
        for row in rows(target,table):
            if reader.execute('SELECT 1 FROM '+table+' WHERE '+key+'=?',(row[key],)).fetchone():
                continue
            subject = 'e.recording_id' if table=='recordings' else 'e.video_id'
            entry = target.execute('SELECT e.video_id FROM catalog_entries e WHERE '+subject+'=?',
                                   (row[key],)).fetchone()
            if entry is None or entry[0] not in admitted:
                raise ValueError('Direct rebuild admitted an unrecorded catalog owner: '+table)
    if 'taste_axes' in source.tables(reader):
        for row in rows(target,'taste_axes'):
            if reader.execute('SELECT 1 FROM taste_axes WHERE id=?',(row['id'],)).fetchone():
                continue
            found = target.execute('SELECT 1 FROM playlist_recipe_facets WHERE axis_id=? UNION ALL '
                'SELECT 1 FROM taste_checkpoint_weights WHERE axis_id=? LIMIT 1',(row['id'],)*2).fetchone()
            if found is None:
                raise ValueError('Direct rebuild admitted an unowned taste axis')
    # Added scopes must have an explicit durable collection owner. Retained
    # scopes and their captured account/provider identity were checked above.
    for row in rows(target,'playlist_scopes'):
        if reader.execute('SELECT 1 FROM playlist_scopes WHERE ref=?',(row['id'],)).fetchone():
            continue
        owners = (('action_targets', 'playlist_scope_id'), ('action_steps', 'created_playlist_scope_id'),
            ('radio_sessions', 'playlist_scope_id'), ('radio_playlist_origins', 'playlist_scope_id'),
            ('listening_playback_bindings', 'playlist_scope_id'), ('album_playback_intents', 'playlist_scope_id'),
            ('sync_cursors', 'playlist_scope_id'), ('rec_recipes', 'playlist_scope_id'),
            ('cluster_canvas', 'playlist_scope_id'))
        found = target.execute(' UNION ALL '.join('SELECT 1 FROM '+table+' WHERE '+key+'=?'
            for table,key in owners)+' LIMIT 1',(row['id'],)*len(owners)).fetchone()
        if found is None:
            raise ValueError('Direct rebuild admitted an unowned playlist scope')
