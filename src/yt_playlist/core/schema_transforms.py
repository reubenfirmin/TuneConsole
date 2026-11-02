"""Finite installed DDL contracts, independent of historical upgrade execution."""
from pathlib import Path
import re
import sqlite3

from .domain_contracts import CONTRACTS
from .schema_contract import MigrationError, TOKEN


COLLECTION_DEFINITIONS = ('schema_collection_state.sql','schema_session_state.sql','schema_sync_state.sql','schema_model_state.sql')
COLLECTION_KEYS = frozenset({'album_feedback','album_menu_tuning','auth_expired_identities','last_sync_at',
    'last_sync_attempt_at','last_sync_outcome','library_sync_state','listening_album_pending',
    'listening_playback_bindings','radio_current_session','radio_playlist_a_ytm','radio_playlist_b_ytm',
    'radio_playlist_ytm','radio_session_playlist_ids','rec_artist_content_model','rec_artist_fit_inputs',
    'rec_candidate_preferences','rec_candidates_expire_at','rec_content_generation','rec_content_genre_revision',
    'rec_content_inputs','rec_content_model','rec_fit_inputs','rec_group_shadow_checkpoint',
    'rec_group_shadow_progress','rec_mode_fit_inputs','rec_preparation_basis','rec_preparation_fit',
    'rec_preparation_full','rec_selection_active_run','rec_selection_display_run','rec_selection_live_run',
    'rec_selection_restore','rec_vibe_version'})
TASTE_TABLES = ('rec_weights','rec_lean','home_hidden_facet','rec_theme','rec_play_grad','rec_grad_log')
TASTE_RENAMES = {'rec_play_grad':'play_gradient_checkpoints','rec_grad_log':'recommendation_gradient_events'}
ALBUM_RETIRED = {'album_tracks','album_fetch_state','album_share_snapshots','album_tracklists','album_track_links',
    'ix_album_track_entry','ix_album_track_recording','ix_album_track_incidental','ix_rec_album_cursor','album_track_links_delete'}
LISTENING_REPLACED = {'play_events','listening_observations','trend_first_play',
    'play_events_subject','play_events_listening_insert','ix_play_events_key','ix_play_events_key_time',
    'ix_play_event_account_key_time','ix_listening_observation_subject_day','ix_listening_observation_source_scope'}
DISCOVERY_REPLACED = {'artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',
    'discovery_visible_impressions','ix_artist_scans_artist','ix_discovered_artists_artist',
    'discovery_artist_sources_identity','discovered_tracks_delete'}
CATALOG_RETIRED = {'catalog_tracks_update','tracks_update','discovered_tracks_update',
    'discovered_tracks_delete','playlist_tracks_insert','playlist_tracks_delete'}
OBJECT = re.compile(r'CREATE (?:TABLE|VIEW|(?:UNIQUE )?INDEX|TRIGGER)\s+"?(\w+)"?',re.I)


def taste_axes(sql):
    table = re.match(r'CREATE TABLE\s+"?(\w+)"?\s*\(',sql.lstrip(),re.I)
    if table and table[1] in TASTE_TABLES:
        sql = re.sub(r'(?<!\w)"?(axis|facet)"?\s+TEXT\b','axis_id INTEGER REFERENCES taste_axes(id)',sql,count=1,flags=re.I)
        for old,new in TASTE_RENAMES.items():
            sql = re.sub(r'\b'+old+r'\b',new,sql)
    return sql


def recipe_memberships(sql):
    if re.match(r'CREATE TABLE\s+"?road_trip_recipes"?\s*\(',sql.lstrip(),re.I):
        sql = re.sub(r'\b(?:artists|genres|blacklist_genres)\s+TEXT\s+NOT NULL\s*,(?:[ \t]*--[^\n]*)?','',sql)
    return sql


def collection_state(sql):
    return '' if re.match(r'CREATE TABLE\s+"?road_trip_drafts"?\s*\(',sql.lstrip(),re.I) else sql


def collection_documents(sql):
    return '' if re.match(r'CREATE TABLE\s+"?(rec_recipes|cluster_canvas)"?\s*\(',sql.lstrip(),re.I) else sql


def action_work(sql):
    if (re.match(r'CREATE TABLE\s+"?actions"?\s*\(',sql.lstrip(),re.I)
            or re.match(r'CREATE INDEX ix_actions_delete_pending\b',sql.lstrip(),re.I)
            or re.match(r'CREATE TRIGGER playlist_revision_actions_(insert|update|delete)\b',sql.lstrip(),re.I)):
        return ''
    return sql


def album_occurrences(sql):
    match = OBJECT.match(sql.lstrip())
    if match and (match[1] in ALBUM_RETIRED or re.match(r'rec_(?:capture|evidence)_album_tracks_',match[1])):
        return ''
    if match and match[1]=='albums' and re.match(r'CREATE TABLE\b',sql.lstrip(),re.I):
        end = sql.rindex(')')
        return sql[:end]+',current_tracklist_id INTEGER,FOREIGN KEY(current_tracklist_id,id) REFERENCES album_tracklists(id,album_id) DEFERRABLE INITIALLY DEFERRED'+sql[end:]
    return sql


def listening_subjects(sql):
    match = OBJECT.match(sql.lstrip())
    if match and match[1] in LISTENING_REPLACED:
        return ''
    if re.match(r'CREATE VIEW\b',sql.lstrip(),re.I):
        sql = re.sub(r'\b(FROM|JOIN) play_events\b',r'\1 play_event_subjects',sql)
        sql = re.sub(r'\b(FROM|JOIN) listening_observations\b',r'\1 listening_observation_subjects',sql)
    if match and re.match(r'CREATE TRIGGER\b',sql.lstrip(),re.I) and re.search(r' ON (play_events|listening_observations)\b',sql):
        sql = sql.replace('OLD.identity_key','OLD.unresolved_subject_key').replace('NEW.identity_key','NEW.unresolved_subject_key')
    return sql


def discovery_subjects(sql):
    match = OBJECT.match(sql.lstrip())
    if match and match[1] in DISCOVERY_REPLACED:
        return ''
    if match and match[1]=='discovered_tracks':
        sql = sql.replace('(SELECT source_token FROM discovery_sources s WHERE s.recording_id=d.recording_id ORDER BY s.found_at,source_token LIMIT 1)',
            '(SELECT o.source_token FROM discovery_sources s JOIN discovery_origin_tokens o ON o.id=s.origin_id '
            'WHERE s.recording_id=d.recording_id ORDER BY s.first_found_at,o.source_token,s.id LIMIT 1)')
    if match and match[1]=='genre_classification_legacy':
        sql = sql.replace('REFERENCES discovered_artists(artist_id)','REFERENCES artists(id)')
    return sql


def catalog_writes(sql):
    match = re.match(r'CREATE TRIGGER\s+"?(\w+)"?',sql.lstrip(),re.I)
    return '' if match and match[1] in CATALOG_RETIRED else sql


def entity_names(sql):
    identifiers = {match.group().strip('"`[]').lower() for match in TOKEN.finditer(sql)
                   if not match.group().startswith(("'",'--','/*'))}
    names = {'identities':'accounts','identity_id':'account_id','scope_ref':'playlist_scope_id',
             'ytm_playlist_id':'provider_playlist_id','playlist_ref':'playlist_scope_id',
             'ix_playlist_scope':'ix_playlist_scope_account'}
    if 'playlist_scopes' in identifiers:
        names.update(ref='id',provider_id='provider_playlist_id')
    if identifiers & {'cleanup_ignored','quarantined_playlists'}:
        names['ytm'] = 'playlist_scope_id'
    if identifiers & {'suppressed_overlaps','overlap_kept'}:
        names.update(a='first_playlist_scope_id',b='second_playlist_scope_id')
    if 'rec_mode_picks' in identifiers:
        names['playlist_id'] = 'membership_ref_id'
    def replace(match):
        token = match.group()
        if token.startswith(("'",'--','/*')):
            return token
        quoted = token.startswith(('"','`','['))
        key = token[1:-1] if quoted else token
        value = names.get(key.lower())
        return token if value is None else token[0]+value+token[-1] if quoted else value
    return TOKEN.sub(replace,sql)


def domain_contracts(sql):
    match = re.match(r'CREATE TABLE\s+["`\[]?(\w+)["`\]]?\s*\(',sql.lstrip(),re.I)
    if match is None or match[1] not in CONTRACTS:
        return sql
    checks = ''.join(f',CONSTRAINT ck_{match[1]}_{column} CHECK({expression})'
                     for column,expression in CONTRACTS[match[1]].items())
    closing = sql.rfind(')')
    return sql[:closing]+checks+sql[closing:]


def collection_fences():
    keys = ','.join("'"+key.replace("'","''")+"'" for key in sorted(COLLECTION_KEYS))
    prefixes = ' OR '.join(f"substr(NEW.key,1,{len(prefix)})='{prefix}'" for prefix in
                           ('enrich_provider_refresh:','discovery:artist-image:','discovery_tracks_fetched:'))
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER collection_settings_{event.lower()} BEFORE {event} ON settings '
               f'WHEN NEW.key IN ({keys}) OR {prefixes} '
               "BEGIN SELECT RAISE(ABORT,'Collection state requires its domain owner'); END;")


def choice_fences():
    children = (('choice_learning_parameters',None),('choice_learning_exclusions',None),
        ('choice_mode_estimates','choice_bandit'),('choice_group_estimates','group_choice_bandit'),
        ('choice_group_opportunities','group_choice_bandit'),('choice_learning_group_roots','group_choice_bandit'),
        ('choice_interest_contexts','choice_interests'),('choice_interest_features','choice_interests'),
        ('choice_interest_tracks','choice_interests'),('choice_album_interests','choice_interests'),
        ('choice_album_track_days','choice_interests'))
    for table,surface in children:
        owner = f" AND surface='{surface}'" if surface else " AND surface IN ('choice_bandit','group_choice_bandit')"
        for event in ('INSERT','UPDATE'):
            yield (f'CREATE TRIGGER {table}_owner_{event.lower()} BEFORE {event} ON {table} WHEN NOT EXISTS('
                   f'SELECT 1 FROM choice_learning_publications WHERE id=NEW.publication_id{owner}) '
                   "BEGIN SELECT RAISE(ABORT,'Wrong choice learning owner'); END;")
        for event in ('INSERT','UPDATE','DELETE'):
            references = ('NEW',) if event=='INSERT' else ('OLD',) if event=='DELETE' else ('NEW','OLD')
            condition = ' OR '.join(f'publication_id={reference}.publication_id' for reference in references)
            yield (f'CREATE TRIGGER {table}_{event.lower()} BEFORE {event} ON {table} WHEN EXISTS('
                   f'SELECT 1 FROM choice_learning_heads WHERE {condition}) '
                   "BEGIN SELECT RAISE(ABORT,'Published choice learning is immutable'); END;")
    for event in ('UPDATE','DELETE'):
        yield (f'CREATE TRIGGER choice_learning_publications_{event.lower()} BEFORE {event} '
               'ON choice_learning_publications WHEN EXISTS(SELECT 1 FROM choice_learning_heads '
               "WHERE publication_id=OLD.id) BEGIN SELECT RAISE(ABORT,'Choice learning is retained'); END;")


def retired_recommendation_fences():
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER retired_recommendation_bucket_{event.lower()} BEFORE {event} ON rec_proposals '
               "BEGIN SELECT RAISE(ABORT,'Recommendation state requires its domain owner'); END")


def taste_control_fences():
    keys = ','.join("'"+key+"'" for key in sorted(
        {'taste_settings_checkpoint_v1','taste_comparison_baseline_v2','ppr_experiment_decisions'}))
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER taste_control_settings_{event.lower()} BEFORE {event} ON settings '
               f'WHEN NEW.key IN ({keys}) BEGIN '
               "SELECT RAISE(ABORT,'Taste controls require their domain owner'); END")


def document_fences():
    yield ("CREATE TRIGGER spotify_import_job_account_update BEFORE UPDATE ON spotify_import_jobs "
           "WHEN NEW.account_id IS NOT OLD.account_id OR NEW.unresolved_account_id IS NOT OLD.unresolved_account_id "
           "OR NEW.account_present IS NOT OLD.account_present "
           "BEGIN SELECT RAISE(ABORT,'Spotify import job account cannot change'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER spotify_import_settings_{event.lower()} BEFORE {event} ON settings '
               "WHEN NEW.key='spotify_library_import_job' BEGIN "
               "SELECT RAISE(ABORT,'Spotify import state requires its job owner'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER playlist_recipe_facet_{event.lower()} BEFORE {event} ON playlist_recipe_facets '
               "WHEN NOT EXISTS (SELECT 1 FROM taste_axes a WHERE a.id=NEW.axis_id AND "
               "((NEW.kind='genre' AND a.kind IN ('genre','family','unresolved_genre')) OR "
               "(NEW.kind='era' AND a.kind='other' AND a.value='era:'||NEW.captured_value))) "
               "BEGIN SELECT RAISE(ABORT,'Playlist recipe facet axis disagrees with its kind'); END")


def saved_output_definitions():
    pending = ''
    path = Path(__file__).with_name('migrations') / 'schema_recommendation_setting_outputs.sql'
    for line in path.read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete saved recommendation output schema')
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER saved_recommendation_settings_{event.lower()} BEFORE {event} ON settings '
               "WHEN NEW.key IN ('radio_seed_offer','rec_autotune_result') BEGIN "
               "SELECT RAISE(ABORT,'Saved recommendation output requires its domain owner'); END")
        yield (f'CREATE TRIGGER radio_seed_subject_{event.lower()} BEFORE {event} ON radio_seed_subjects '
               'WHEN NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog_entries '
               'WHERE id=NEW.entry_id AND video_id=NEW.video_id) BEGIN '
               "SELECT RAISE(ABORT,'Radio seed does not match its catalog entry'); END")
    for table,masks in (('model_comparison_plan',{'policy_mask':2047,'previous_mask':3,'challenger_mask':3}),
                        ('model_listening_monitor',{'policy_mask':2047,'setup_mask':3}),
                        ('model_album_assignment',{'comparison_mask':1023})):
        invalid = ' OR '.join(f'NEW.{field}>{maximum}' for field,maximum in masks.items())
        if table=='model_album_assignment':
            invalid += " OR (typeof(NEW.epoch)='text' AND length(NEW.epoch) NOT BETWEEN 1 AND 4096)"
        for event in ('INSERT','UPDATE'):
            yield (f'CREATE TRIGGER {table}_fields_{event.lower()} BEFORE {event} ON {table} WHEN {invalid} '
                   "BEGIN SELECT RAISE(ABORT,'Invalid model comparison fields'); END")


def listening_fences():
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER play_event_membership_{event.lower()} BEFORE {event} ON play_events '
            'WHEN NEW.membership_ref_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_membership_refs m '
            'JOIN playlist_scopes s ON s.ref=m.scope_ref WHERE m.id=NEW.membership_ref_id AND m.identity_id=NEW.identity_id '
            "AND (NEW.playlist_ytm_id=s.provider_id OR NEW.playlist_ytm_id='RDAMPL'||s.provider_id)) "
            "BEGIN SELECT RAISE(ABORT,'Listening membership disagrees with captured account or playlist'); END")
    for table in ('play_events','play_event_sources','play_event_calendar','listening_observations',
                  'listening_calendars','catalog_entries','entry_artists','artist_redirects','recording_redirects'):
        for event in ('INSERT','UPDATE','DELETE'):
            yield (f'CREATE TRIGGER listening_summary_{table}_{event.lower()} AFTER {event} ON {table} BEGIN '
                'INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
                'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')
    for table in ('artists','recordings'):
        yield (f'CREATE TRIGGER listening_summary_{table}_update AFTER UPDATE ON {table} BEGIN '
            'INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
            'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')


def discovery_fences():
    yield ('CREATE TRIGGER discovered_tracks_delete INSTEAD OF DELETE ON discovered_tracks BEGIN '
           'DELETE FROM discovery_memberships WHERE recording_id=OLD.recording_id; END')


def model_control_statements():
    number = "REAL CHECK({0} IS NULL OR (typeof({0}) IN ('integer','real') AND abs({0})<=1.7976931348623157e308))"
    integer = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0}>=0))"
    text = "TEXT CHECK({0} IS NULL OR (length({0}) BETWEEN 1 AND 4096))"
    flag = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0} IN (0,1)))"
    policy = [('mask',integer),('version',text),('horizon_days',integer),('followup_hours',number),
        ('min_days',integer),('min_observed',integer),('min_coverage',number),('useful_effect',number),
        ('format_margin',number),('min_format_days',integer),('retry_days',integer),('family_alpha',number)]
    setup = [('mask',integer),('method',text),('dim',integer)]
    plan = [('phase',"TEXT CHECK(phase IS NULL OR phase IN ('preparing','observing','failed','applied','retained'))"),
        ('attempt',integer),('seed',text),('alpha',number),('constraints',text),('run_id',integer),
        ('started_at',number),('exposure_until',number),('decision_at',number),('vectors',text),
        ('generation',text),('prepared_at',number),('last_prepare_attempt',number),
        ('runtime_seconds',number),('retry_at',number),('reason',text),('ended_at',number)]
    plan += [('policy_'+name,kind) for name,kind in policy]
    plan += [(prefix+'_'+name,kind) for prefix in ('previous','challenger') for name,kind in setup]
    monitor = [('run_id',integer),('generation',text),('started_at',number),('exposure_until',number),
        ('next_at',number),('baseline_present',flag),('declining_weeks',integer),('complete',flag)]
    monitor += [('policy_'+name,kind) for name,kind in policy]+[('setup_'+name,kind) for name,kind in setup]
    assignment = [('epoch',"CHECK(epoch IS NULL OR typeof(epoch) IN ('integer','text'))"),('comparison_mask',integer),
        ('comparison_run_id',integer),('comparison_arm',"TEXT CHECK(comparison_arm IS NULL OR comparison_arm IN ('incumbent','challenger'))"),
        ('comparison_probability',"REAL CHECK(comparison_probability IS NULL OR comparison_probability BETWEEN 0 AND 1)"),
        ('comparison_unit',text),('comparison_format',"TEXT CHECK(comparison_format IS NULL OR comparison_format IN ('mixes','albums','radio'))"),
        ('comparison_window',text),('comparison_policy',text),('comparison_generation',text),
        ('comparison_at',number),('comparison_controlled',flag)]
    for table,fields,maximum,raw_run in (
        ('model_comparison_plan',plan,1048575,'run_id'),('model_listening_monitor',monitor,1023,'run_id'),
        ('model_album_assignment',assignment,3,'comparison_run_id')):
        columns = ['id INTEGER PRIMARY KEY NOT NULL CHECK(id=1)',
            "field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND "+str(maximum)+')',
            'run_ref INTEGER REFERENCES rec_model_selection_runs(id)','run_resolution TEXT','built_at '+number.format('built_at')]
        columns.extend('"'+name+'" '+kind.format('"'+name+'"') for name,kind in fields)
        columns.append(f'CHECK(({raw_run} IS NULL AND run_ref IS NULL AND run_resolution IS NULL) OR '
            f'({raw_run} IS NOT NULL AND ((run_ref IS NOT NULL AND run_ref={raw_run} AND run_resolution IS NULL) OR '
            "(run_ref IS NULL AND run_resolution IS NOT NULL AND run_resolution='missing_run'))))")
        yield 'CREATE TABLE '+table+' ('+','.join(columns)+')'
    yield ("CREATE TABLE model_monitor_baseline (monitor_id INTEGER NOT NULL REFERENCES model_listening_monitor(id),"
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 4095),"
           "value REAL NOT NULL CHECK(typeof(value) IN ('integer','real') AND value BETWEEN 0 AND 1),"
           'PRIMARY KEY(monitor_id,position)) WITHOUT ROWID')


def album_fences():
    useful = ("EXISTS(SELECT 1 FROM album_tracklists t WHERE t.id=NEW.current_tracklist_id AND t.album_id=NEW.id "
        "AND t.status='complete' AND t.expected_count>0 AND t.expected_count="
        '(SELECT count(*) FROM album_track_occurrences o WHERE o.tracklist_id=t.id))')
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER album_current_tracklist_{event.lower()} BEFORE {event} ON albums '
               f'WHEN NEW.current_tracklist_id IS NOT NULL AND NOT ({useful}) '
               "BEGIN SELECT RAISE(ABORT,'Album current tracklist is not a complete owned publication'); END")
        yield (f'CREATE TRIGGER album_source_agreement_{event.lower()} BEFORE {event} ON album_occurrence_sources '
            'WHEN NOT EXISTS(SELECT 1 FROM album_track_occurrences o JOIN catalog_entries e ON e.id=NEW.entry_id '
            'WHERE o.id=NEW.occurrence_id AND o.recording_id=e.recording_id AND '
            "((NEW.provider='youtube' AND e.video_id IS NOT NULL) OR (NEW.provider='local' AND e.source_ref LIKE 'local:%') OR "
            "(NEW.provider='unresolved' AND e.source_ref IS NOT NULL AND e.source_ref NOT LIKE 'local:%'))) "
            "BEGIN SELECT RAISE(ABORT,'Album occurrence source disagrees with its subject or provider'); END")
        yield (f'CREATE TRIGGER album_tracklist_provider_{event.lower()} BEFORE {event} ON album_tracklists '
            'WHEN NOT EXISTS(SELECT 1 FROM album_provider_ids p WHERE p.provider=NEW.provider AND p.provider_id=NEW.provider_id AND p.album_id=NEW.album_id) '
            "BEGIN SELECT RAISE(ABORT,'Album tracklist belongs to another provider representation'); END")
        yield (f'CREATE TRIGGER album_share_source_{event.lower()} BEFORE {event} ON album_share_items '
            'WHEN NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM album_occurrence_sources s '
            'WHERE s.occurrence_id=NEW.occurrence_id AND s.entry_id=NEW.entry_id) '
            "BEGIN SELECT RAISE(ABORT,'Frozen album source does not belong to its occurrence'); END")
    yield ("CREATE TRIGGER album_tracklist_finish BEFORE UPDATE ON album_tracklists WHEN NEW.status IN ('complete','snapshot') AND ("
        'NEW.expected_count IS NULL OR NEW.expected_count<>(SELECT count(*) FROM album_track_occurrences WHERE tracklist_id=NEW.id) OR '
        "(NEW.status='complete' AND NEW.expected_count=0) OR (NEW.order_status='known' AND EXISTS(SELECT 1 FROM album_track_occurrences WHERE tracklist_id=NEW.id AND position IS NULL)) OR "
        "(NEW.order_status='unknown' AND EXISTS(SELECT 1 FROM album_track_occurrences WHERE tracklist_id=NEW.id AND position IS NOT NULL)) OR "
        "(NEW.order_status='known' AND NEW.expected_count>0 AND ((SELECT min(position) FROM album_track_occurrences WHERE tracklist_id=NEW.id)<>0 OR "
        '(SELECT max(position) FROM album_track_occurrences WHERE tracklist_id=NEW.id)<>NEW.expected_count-1))) '
        "BEGIN SELECT RAISE(ABORT,'Album tracklist count or captured order is incomplete'); END")
    yield ("CREATE TRIGGER album_tracklist_initial BEFORE INSERT ON album_tracklists WHEN NEW.status<>'building' "
           "BEGIN SELECT RAISE(ABORT,'Album tracklists must be built before publication'); END")
    yield ("CREATE TRIGGER album_tracklist_frozen BEFORE UPDATE ON album_tracklists WHEN OLD.status<>'building' AND "
        '(NEW.id<>OLD.id OR NEW.provider<>OLD.provider OR NEW.provider_id<>OLD.provider_id OR NEW.revision<>OLD.revision OR NEW.capture_kind<>OLD.capture_kind OR '
        'NEW.status<>OLD.status OR NEW.order_status<>OLD.order_status OR NEW.expected_count IS NOT OLD.expected_count OR NEW.fetched_at IS NOT OLD.fetched_at) '
        "BEGIN SELECT RAISE(ABORT,'Published album capture cannot change'); END")
    for event,side in (('INSERT','NEW'),('DELETE','OLD')):
        retained = ('' if event=='INSERT' else " AND (capture_kind<>'provider' OR EXISTS(SELECT 1 FROM albums a WHERE a.current_tracklist_id=album_tracklists.id) "
            'OR EXISTS(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=album_tracklists.id))')
        yield (f'CREATE TRIGGER album_occurrence_frozen_{event.lower()} BEFORE {event} ON album_track_occurrences '
            f"WHEN EXISTS(SELECT 1 FROM album_tracklists WHERE id={side}.tracklist_id AND status<>'building'{retained}) "
            "BEGIN SELECT RAISE(ABORT,'Published album occurrences are retained'); END")
    yield ("CREATE TRIGGER album_occurrence_frozen_update BEFORE UPDATE ON album_track_occurrences WHEN "
        "EXISTS(SELECT 1 FROM album_tracklists WHERE id=OLD.tracklist_id AND status<>'building') AND "
        '(NEW.id<>OLD.id OR NEW.tracklist_id<>OLD.tracklist_id OR NEW.position IS NOT OLD.position OR NEW.disc_number IS NOT OLD.disc_number OR '
        'NEW.track_number IS NOT OLD.track_number OR NEW.captured_title IS NOT OLD.captured_title OR NEW.captured_credit IS NOT OLD.captured_credit OR '
        'NEW.raw_identity_key IS NOT OLD.raw_identity_key OR NEW.raw_source_ref IS NOT OLD.raw_source_ref OR NEW.captured_source IS NOT OLD.captured_source OR NEW.legacy_row_id IS NOT OLD.legacy_row_id) '
        "BEGIN SELECT RAISE(ABORT,'Published album occurrence capture cannot change'); END")
    yield ("CREATE TRIGGER album_occurrence_resolution BEFORE UPDATE ON album_track_occurrences WHEN EXISTS("
        'SELECT 1 FROM album_occurrence_sources s JOIN catalog_entries e ON e.id=s.entry_id '
        'WHERE s.occurrence_id=NEW.id AND e.recording_id IS NOT NEW.recording_id) '
        "BEGIN SELECT RAISE(ABORT,'Album occurrence resolution disagrees with its sources'); END")
    yield ("CREATE TRIGGER album_source_reassociation AFTER UPDATE OF recording_id ON catalog_entries WHEN NEW.recording_id<>OLD.recording_id BEGIN "
        'UPDATE album_track_occurrences SET recording_id=NEW.recording_id WHERE id IN '
        '(SELECT occurrence_id FROM album_occurrence_sources WHERE entry_id=NEW.id); END')
    for event in ('UPDATE','DELETE'):
        retained = ('' if event=='UPDATE' else " AND (t.capture_kind<>'provider' OR EXISTS(SELECT 1 FROM albums a WHERE a.current_tracklist_id=t.id) "
            'OR EXISTS(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=t.id))')
        yield (f'CREATE TRIGGER album_source_frozen_{event.lower()} BEFORE {event} ON album_occurrence_sources '
            f"WHEN EXISTS(SELECT 1 FROM album_track_occurrences o JOIN album_tracklists t ON t.id=o.tracklist_id WHERE o.id=OLD.occurrence_id AND t.status<>'building'{retained}) "
            "BEGIN SELECT RAISE(ABORT,'Published album source evidence is retained'); END")
    for table in ('album_share_snapshots','album_share_items'):
        condition = ' WHEN NEW.id<>OLD.id OR NEW.tracklist_id<>OLD.tracklist_id OR NEW.captured_provider_id<>OLD.captured_provider_id OR NEW.title<>OLD.title OR NEW.created_at IS NOT OLD.created_at' if table=='album_share_snapshots' else ''
        yield (f'CREATE TRIGGER {table}_frozen BEFORE UPDATE ON {table}{condition} '
               "BEGIN SELECT RAISE(ABORT,'Frozen album share cannot change'); END")
        yield (f'CREATE TRIGGER {table}_retained BEFORE DELETE ON {table} '
               "BEGIN SELECT RAISE(ABORT,'Frozen album share is retained'); END")
    for table in ('album_item_observations','album_track_occurrences','album_occurrence_sources'):
        for event in ('INSERT','UPDATE','DELETE'):
            yield (f'CREATE TRIGGER rec_capture_{table}_{event.lower()} AFTER {event} ON {table} BEGIN '
                "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; "
                'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')
    yield ("CREATE TRIGGER rec_capture_album_publication AFTER UPDATE OF current_tracklist_id ON albums WHEN OLD.current_tracklist_id IS NOT NEW.current_tracklist_id BEGIN "
        "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; "
        'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')


def action_fences():
    yield ("CREATE TRIGGER action_identity BEFORE UPDATE ON actions WHEN NEW.id<>OLD.id OR NEW.kind<>OLD.kind "
        'OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.intent_digest<>OLD.intent_digest OR '
        'NEW.parent_action_id IS NOT OLD.parent_action_id OR NEW.undo_of_action_id IS NOT OLD.undo_of_action_id OR '
        'NEW.created_at IS NOT OLD.created_at OR NEW.execution_mode IS NOT OLD.execution_mode OR '
        '(OLD.reviewed_draft_id IS NOT NULL AND (NEW.reviewed_draft_id IS NOT OLD.reviewed_draft_id OR '
        'NEW.reviewed_draft_revision IS NOT OLD.reviewed_draft_revision OR NEW.reviewed_draft_signature IS NOT OLD.reviewed_draft_signature)) '
        "BEGIN SELECT RAISE(ABORT,'Committed action identity and reviewed revision cannot change'); END")
    transitions = {
        'draft':('cancelled',),'planned':('executing','executed','delete_pending','delete_failed','failed','cancelled'),
        'pending':('executing','executed','failed','cancelled'),'executing':('executed','delete_pending','delete_failed','failed'),
        'executed':('undoing','delete_pending','delete_failed'),'delete_pending':('executed','delete_failed'),
        'delete_failed':('delete_pending','executed','undoing'),'undoing':('undo_failed','undone'),
        'undo_failed':('undoing',),'failed':('executing','cancelled'),'cancelled':(),'undone':(),
    }
    allowed = ' OR '.join("(OLD.status='"+old+"' AND NEW.status IN ("+','.join("'"+state+"'" for state in states)+'))'
                          for old,states in transitions.items() if states)
    yield ('CREATE TRIGGER action_status_transition BEFORE UPDATE ON actions WHEN NEW.status<>OLD.status AND NOT ('+allowed+
        ") BEGIN SELECT RAISE(ABORT,'Invalid action status transition'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER action_reviewed_draft_{event.lower()} BEFORE {event} ON actions '
            'WHEN NEW.reviewed_draft_id IS NOT NULL '+('AND OLD.reviewed_draft_id IS NULL ' if event=='UPDATE' else '')+
            'AND NOT EXISTS (SELECT 1 FROM action_drafts d WHERE d.id=NEW.reviewed_draft_id '
            'AND d.revision=NEW.reviewed_draft_revision AND d.signature=NEW.reviewed_draft_signature) '
            "BEGIN SELECT RAISE(ABORT,'Reviewed draft revision is no longer current'); END")
        yield (f'CREATE TRIGGER action_draft_owner_{event.lower()} BEFORE {event} ON action_drafts '
            "WHEN NOT EXISTS (SELECT 1 FROM actions a WHERE a.id=NEW.id AND a.kind='merge_draft' AND a.status='draft') "
            "BEGIN SELECT RAISE(ABORT,'Mutable preparation requires its draft action'); END")
    agreement = ("NOT EXISTS (SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.playlist_scope_id "
        "AND s.provider_id=NEW.provider_playlist_id_snapshot AND "
        "((s.kind='shared' AND s.identity_id IS NULL) OR (s.kind='account' AND s.identity_id=NEW.account_id)))")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER action_target_agreement_{event.lower()} BEFORE {event} ON action_targets '
               f'WHEN (NEW.playlist_scope_id IS NOT NULL AND ({agreement})) OR '
               '(NEW.membership_ref_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM playlist_membership_refs m '
               'WHERE m.id=NEW.membership_ref_id AND m.scope_ref IS NEW.playlist_scope_id AND m.identity_id IS NEW.account_id)) '
               "BEGIN SELECT RAISE(ABORT,'Action target disagrees with its account, scope or membership'); END")
    yield ("CREATE TRIGGER action_target_immutable BEFORE UPDATE ON action_targets "
           "BEGIN SELECT RAISE(ABORT,'Committed action targets are immutable'); END")
    yield ("CREATE TRIGGER action_history_delete BEFORE DELETE ON actions WHEN OLD.kind<>'merge_draft' "
           "BEGIN SELECT RAISE(ABORT,'Action history is retained for recovery and links'); END")
    yield ("CREATE TRIGGER action_step_identity BEFORE UPDATE ON action_steps WHEN NEW.action_id<>OLD.action_id "
           "OR NEW.target_id IS NOT OLD.target_id OR NEW.position<>OLD.position OR NEW.operation<>OLD.operation "
           "BEGIN SELECT RAISE(ABORT,'Committed action step identity cannot change'); END")
    yield ("CREATE TRIGGER action_step_completion BEFORE UPDATE ON action_steps WHEN OLD.phase='complete' "
           "AND NEW.phase<>'complete' BEGIN SELECT RAISE(ABORT,'A completed recovery step cannot reopen'); END")
    yield ("CREATE TRIGGER action_step_creation BEFORE UPDATE ON action_steps "
           'WHEN (OLD.creation_marker IS NOT NULL AND NEW.creation_marker IS NOT OLD.creation_marker) OR '
           '(OLD.created_provider_id_snapshot IS NOT NULL AND NEW.created_provider_id_snapshot IS NOT OLD.created_provider_id_snapshot) '
           "BEGIN SELECT RAISE(ABORT,'A committed recovery creation cannot change its marker or destination'); END")
    step_transitions = {
        'prepared':('pending','cancelled'),
        'pending':('uncertain','failed','creating','created','restoring','deleting','remote_complete','complete','cancelled'),
        'uncertain':('failed','remote_complete','complete','restoring'),
        'failed':('pending','uncertain','deleting','restoring','creating','created','remote_complete','complete','cancelled'),
        'creating':('pending','created','failed','remote_complete'),'created':('restoring','remote_complete','complete'),
        'restoring':('remote_complete','failed','complete'),'deleting':('failed','remote_complete','complete'),
        'remote_complete':('complete',),'complete':(),'cancelled':(),
    }
    allowed_steps = ' OR '.join("(OLD.phase='"+old+"' AND NEW.phase IN ("+','.join("'"+state+"'" for state in states)+'))'
                               for old,states in step_transitions.items() if states)
    yield ('CREATE TRIGGER action_step_transition BEFORE UPDATE ON action_steps WHEN NEW.phase<>OLD.phase AND NOT ('+
        allowed_steps+") BEGIN SELECT RAISE(ABORT,'Invalid action step transition'); END")
    yield ("CREATE TRIGGER action_step_backup BEFORE UPDATE ON action_steps WHEN OLD.backup_status='verified' AND "
        '(NEW.backup_status<>OLD.backup_status OR NEW.backup_digest IS NOT OLD.backup_digest OR NEW.backup_bytes IS NOT OLD.backup_bytes) '
        "BEGIN SELECT RAISE(ABORT,'Committed backup content cannot change'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER action_created_scope_{event.lower()} BEFORE {event} ON action_steps '
            'WHEN NEW.created_playlist_scope_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM playlist_scopes s '
            'JOIN action_targets t ON t.id=NEW.target_id WHERE s.ref=NEW.created_playlist_scope_id '
            'AND s.provider_id=NEW.created_provider_id_snapshot AND '
            "(s.kind='shared' OR (s.kind='account' AND s.identity_id=t.account_id))) "
            "BEGIN SELECT RAISE(ABORT,'Recovery creation scope disagrees with its target'); END")
        yield (f'CREATE TRIGGER retired_workflow_settings_{event.lower()} BEFORE {event} ON settings '
            "WHEN substr(NEW.key,1,18)='playlist_gc_retry:' OR NEW.key IN "
            "('spotify_library_imported_playlists','spotify_library_imported_albums') OR "
            "substr(NEW.key,1,35)='spotify_library_imported_playlists:' OR "
            "substr(NEW.key,1,32)='spotify_library_imported_albums:' "
            "BEGIN SELECT RAISE(ABORT,'Workflow settings require their explicit owner'); END")
        for table in ('playlist_gc_attempts','spotify_transfer_receipts'):
            yield (f'CREATE TRIGGER {table}_scope_{event.lower()} BEFORE {event} ON {table} '
                'WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM playlist_scopes s '
                'WHERE s.ref=NEW.playlist_scope_id AND s.provider_id=NEW.provider_playlist_id_snapshot '
                "AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id))) "
                "BEGIN SELECT RAISE(ABORT,'Workflow receipt disagrees with its account and playlist scope'); END")
    yield ("CREATE TRIGGER spotify_transfer_destination BEFORE UPDATE ON spotify_transfer_receipts WHEN "
        'NEW.captured_account_id IS NOT OLD.captured_account_id OR NEW.account_id IS NOT OLD.account_id OR '
        'NEW.source_kind<>OLD.source_kind OR NEW.fingerprint<>OLD.fingerprint OR '
        '(OLD.marker IS NOT NULL AND NEW.marker IS NOT OLD.marker) OR '
        '(OLD.provider_playlist_id_snapshot IS NOT NULL AND NEW.provider_playlist_id_snapshot IS NOT OLD.provider_playlist_id_snapshot) '
        "BEGIN SELECT RAISE(ABORT,'Committed Spotify transfer ownership cannot change'); END")
    yield ("CREATE TRIGGER tv_append_completed BEFORE UPDATE ON tv_append_intents WHEN OLD.state='complete' AND NEW.state<>'complete' "
           "BEGIN SELECT RAISE(ABORT,'Completed TV append cannot reopen'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER tv_append_recipe_{event.lower()} BEFORE {event} ON tv_append_intents '
            'WHEN NEW.recipe_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM rec_recipes r WHERE r.id=NEW.recipe_id '
            'AND r.playlist_scope_id=NEW.playlist_scope_id) '
            "BEGIN SELECT RAISE(ABORT,'TV append belongs to another recipe scope'); END")
        yield (f'CREATE TRIGGER radio_operation_revision_{event.lower()} BEFORE {event} ON radio_operations '
            'WHEN NOT EXISTS(SELECT 1 FROM radio_sessions s WHERE s.id=NEW.session_id AND s.revision=NEW.session_revision) '
            "BEGIN SELECT RAISE(ABORT,'Radio operation belongs to another session revision'); END")
    for event,coordinate in (('INSERT','NEW'),('DELETE','OLD')):
        yield (f'CREATE TRIGGER playlist_revision_action_targets_{event.lower()} AFTER {event} ON action_targets '
               f"WHEN {coordinate}.role='placement' BEGIN INSERT INTO playlist_library_revisions "
               f'SELECT {coordinate}.playlist_scope_id,{coordinate}.account_id,1 FROM actions a '
               f"WHERE a.id={coordinate}.action_id AND a.kind='add_tracks' AND a.status='pending' "
               f'AND {coordinate}.playlist_scope_id IS NOT NULL AND {coordinate}.account_id IS NOT NULL '
               'ON CONFLICT(scope_ref,identity_id) DO UPDATE SET revision=revision+1; END')
    yield ("CREATE TRIGGER playlist_revision_actions_update AFTER UPDATE ON actions "
           "WHEN NEW.kind='add_tracks' AND (OLD.status='pending' OR NEW.status='pending') BEGIN "
           'INSERT INTO playlist_library_revisions SELECT playlist_scope_id,account_id,1 FROM action_targets '
           "WHERE action_id=NEW.id AND role='placement' AND playlist_scope_id IS NOT NULL AND account_id IS NOT NULL "
           'ON CONFLICT(scope_ref,identity_id) DO UPDATE SET revision=revision+1; END')
