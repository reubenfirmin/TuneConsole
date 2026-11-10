"""Finite catalog evidence, typed selection lineage and relational conflicts.

The registered upgrade captures original rows before retiring mutable owners.
The additive installer remains available for conversion and failure fixtures.
Native tag/classifier documents are not a second generic attribute vocabulary.
"""
from .runner import MigrationError

RECORDING = ('genre', 'bpm', 'energy', 'danceability', 'mb_recording_id', 'music_key', 'music_scale',
             'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental')
ITEM = ('title', 'artist', 'orig_title', 'orig_artist', 'album', 'duration_s', 'available', 'video_type',
        'artist_browse_id', 'album_browse_id', 'thumbnail', 'mb_year', 'loudness', 'dynamic_complexity',
        'popularity', 'gain', 'label')
PROBABILITY = ('energy', 'danceability', 'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental')
INTEGER = ('duration_s', 'available', 'popularity')
NUMBER = (*PROBABILITY, *INTEGER, 'bpm', 'loudness', 'dynamic_complexity', 'gain')
TEXT = tuple(field for field in (*RECORDING, *ITEM) if field not in NUMBER)
ORIGINS = ('provider', 'user', 'legacy_finding', 'legacy_log', 'legacy_choice', 'legacy_conflict',
           'legacy_attempt', 'legacy_state', 'legacy_hint', 'selection_snapshot', 'selection_transfer')
ATTEMPT_FIELDS = (*RECORDING, *ITEM, 'year', 'art', 'isrc', 'mb_release_ids', 'cover_url')
ADDED_COLUMNS = {
    'provider_attempts': ('delivery_id INTEGER REFERENCES metadata_deliveries(id)',),
    'recording_identifiers': ('delivery_id INTEGER REFERENCES metadata_deliveries(id)',
                              'observation_id INTEGER REFERENCES metadata_observations(id)'),
}
NATIVE_FIELDS = ('tags', 'bpm', 'energy', 'danceability', 'mood_party', 'mood_aggressive',
                 'music_key', 'music_scale', 'mood_happy', 'mood_sad', 'mood_relaxed',
                 'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity')


def _in(values):
    return '(' + ','.join(repr(value) for value in values) + ')'


def _clock(name):
    return f"CHECK({name} IS NULL OR (typeof({name}) IN ('integer','real') AND {name} BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308))"


def _numeric():
    rules = ["(field='bpm' AND unit='bpm' AND number_value>0)",
             "(field='duration_s' AND unit='seconds' AND typeof(number_value)='integer' AND number_value>=0)",
             "(field='available' AND unit='boolean' AND typeof(number_value)='integer' AND number_value IN (0,1))",
             "(field='popularity' AND unit='provider_score' AND typeof(number_value)='integer')",
             "(field='gain' AND unit='decibels')", "(field='loudness' AND unit='essentia.average_loudness')",
             "(field='dynamic_complexity' AND unit='essentia.dynamic_complexity')",
             "(field='energy' AND unit IN ('legacy_energy_0_1','fraction') AND number_value BETWEEN 0 AND 1)",
             '(field IN ' + _in(tuple(field for field in PROBABILITY if field != 'energy'))
             + " AND unit IN ('classifier_probability','fraction') AND number_value BETWEEN 0 AND 1)"]
    return '(' + ' OR '.join(rules) + ')'


def statements():
    # A legacy input can predate the recorded association/scope. Keep its exact
    # original delivery and the subject that was actually retained; never fill
    # missing historical recording IDs from today's entry association.
    yield ('CREATE TABLE metadata_legacy_records (legacy_owner TEXT NOT NULL CHECK(legacy_owner IN '
           + _in(('metadata_findings', 'enrichment_log', 'metadata_decisions', 'entry_field_choices',
                  'enrichment_conflict', 'recording_identifiers', 'provider_attempts',
                  'enrichment_provider_state', 'catalog_field_hints')) + '),'
           "legacy_id INTEGER NOT NULL CHECK(typeof(legacy_id)='integer'),"
           'delivery_id INTEGER NOT NULL REFERENCES metadata_deliveries(id),'
           'entry_id INTEGER REFERENCES catalog_entries(id),recording_id INTEGER REFERENCES recordings(id),'
           'input_video_id TEXT REFERENCES youtube_items(video_id),'
           "disposition TEXT NOT NULL CHECK(disposition IN ('catalog_evidence','native_observation',"
           "'unsupported_field','invalid_value','unproven_subject','unproven_scope','operational_state')),"
           'PRIMARY KEY(legacy_owner,legacy_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_metadata_legacy_delivery ON metadata_legacy_records(delivery_id)'
    yield ('CREATE TRIGGER owned_metadata_legacy_record BEFORE INSERT ON metadata_legacy_records '
           'WHEN NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=NEW.delivery_id '
           "AND d.key_contract='legacy_record' AND d.raw_filename IS NOT NULL) "
           "BEGIN SELECT RAISE(ABORT,'Legacy metadata requires its retained original delivery'); END")
    yield ('CREATE TRIGGER immutable_metadata_legacy_record BEFORE UPDATE ON metadata_legacy_records '
           "BEGIN SELECT RAISE(ABORT,'Legacy metadata capture is immutable'); END")
    yield ('CREATE TRIGGER retained_metadata_legacy_record BEFORE DELETE ON metadata_legacy_records '
           "BEGIN SELECT RAISE(ABORT,'Legacy metadata capture requires a retention disposition'); END")
    yield ('CREATE TABLE native_metadata_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'delivery_id INTEGER NOT NULL REFERENCES metadata_deliveries(id),'
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),"
           'field TEXT NOT NULL CHECK(field IN ' + _in(NATIVE_FIELDS) + '),'
           "kind TEXT NOT NULL CHECK(kind IN ('reported','measurement','prediction','curated','inherited','derived')),"
           "unit TEXT CHECK(unit IS NULL OR (typeof(unit)='text' AND length(CAST(unit AS BLOB)) BETWEEN 1 AND 256)),"
           'legacy_finding_id INTEGER UNIQUE,UNIQUE(delivery_id,position))')
    yield ('CREATE TRIGGER owned_native_metadata_delivery BEFORE INSERT ON native_metadata_observations '
           'WHEN NEW.legacy_finding_id IS NULL AND NOT EXISTS(SELECT 1 FROM metadata_deliveries d '
           "WHERE d.id=NEW.delivery_id AND d.key_contract IN ('capture','provider_revision') "
           'AND d.input_entry_id IS NOT NULL AND d.input_recording_id IS NOT NULL '
           "AND d.provider_entity_type IN ('recording','release','artist','genre','item')) "
           "BEGIN SELECT RAISE(ABORT,'Native metadata requires its captured source and input'); END")
    yield ('CREATE TRIGGER owned_native_metadata_legacy BEFORE INSERT ON native_metadata_observations '
           'WHEN NEW.legacy_finding_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_legacy_records r '
           "WHERE r.legacy_owner='metadata_findings' AND r.legacy_id=NEW.legacy_finding_id "
           'AND r.delivery_id=NEW.delivery_id) '
           "BEGIN SELECT RAISE(ABORT,'Native metadata requires its retained original'); END")
    yield ('CREATE TRIGGER immutable_native_metadata_observation BEFORE UPDATE ON native_metadata_observations '
           "BEGIN SELECT RAISE(ABORT,'Native metadata evidence is immutable'); END")
    yield ('CREATE TABLE musical_source_observations (source_key TEXT NOT NULL REFERENCES rec_musical_sources(source_key),'
           'field TEXT NOT NULL CHECK(field IN ' + _in(NATIVE_FIELDS) + '),'
           'observation_id INTEGER NOT NULL REFERENCES native_metadata_observations(id),'
           'PRIMARY KEY(source_key,field)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_musical_source_observation ON musical_source_observations(observation_id)'
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER owned_musical_source_observation_{event.lower()} BEFORE {event} ON musical_source_observations '
               'WHEN NOT EXISTS(SELECT 1 FROM native_metadata_observations o '
               'JOIN metadata_deliveries d ON d.id=o.delivery_id JOIN rec_musical_sources s ON s.source_key=NEW.source_key '
               'WHERE o.id=NEW.observation_id AND o.field=NEW.field AND s.provider=d.provider '
               'AND s.scope=d.provider_entity_type AND s.entity_id=d.provider_entity_id) '
               "BEGIN SELECT RAISE(ABORT,'Musical source selection has a different provider entity or field'); END")
    yield ('CREATE TABLE metadata_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'delivery_id INTEGER REFERENCES metadata_deliveries(id),entry_id INTEGER REFERENCES catalog_entries(id),'
           'recording_id INTEGER REFERENCES recordings(id),input_video_id TEXT REFERENCES youtube_items(video_id),'
           "field_scope TEXT NOT NULL CHECK(field_scope IN ('recording','release','item','legacy_item','legacy_recording')),"
           'field TEXT NOT NULL CHECK(field IN ' + _in((*RECORDING, *ITEM)) + '),'
           "value_kind TEXT NOT NULL CHECK(value_kind IN ('value','null','blank','unresolved','withdrawn')),"
           'text_value TEXT CHECK(text_value IS NULL OR length(CAST(text_value AS BLOB))<=65536),'
           # No numeric affinity: preserve exact integers, reject stored text,
           # and avoid silently rounding large integer evidence into a REAL.
           "number_value CHECK(number_value IS NULL OR (typeof(number_value) IN ('integer','real') AND "
           'number_value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)),'
           'genre_key TEXT REFERENCES genre_catalog(key),unit TEXT,resolution_reason TEXT '
           'CHECK(resolution_reason IS NULL OR length(resolution_reason)>0),'
           'observed_at REAL ' + _clock('observed_at') + ','
           'origin TEXT NOT NULL CHECK(origin IN ' + _in(ORIGINS) + '),legacy_source TEXT,'
           'source_run_id TEXT CHECK(source_run_id IS NULL OR length(CAST(source_run_id AS BLOB))<=4096),'
           'legacy_reference TEXT CHECK(legacy_reference IS NULL OR length(CAST(legacy_reference AS BLOB))<=4096),'
           'legacy_owner TEXT CHECK(legacy_owner IS NULL OR legacy_owner IN '
           + _in(('metadata_findings', 'enrichment_log', 'metadata_decisions', 'entry_field_choices',
                  'enrichment_conflict', 'recording_identifiers', 'provider_attempts',
                  'enrichment_provider_state', 'catalog_field_hints')) + '),legacy_id INTEGER,'
           "legacy_position INTEGER NOT NULL DEFAULT 0 CHECK(typeof(legacy_position)='integer' AND legacy_position>=0),"
           'derived_from_observation_id INTEGER REFERENCES metadata_observations(id),'
           'FOREIGN KEY(legacy_owner,legacy_id) REFERENCES metadata_legacy_records(legacy_owner,legacy_id),'
           "CHECK((origin='provider' AND delivery_id IS NOT NULL AND legacy_source IS NULL) OR origin<>'provider'),"
           "CHECK(observed_at IS NOT NULL OR origin LIKE 'legacy_%' OR origin='selection_snapshot'),"
           "CHECK((origin='selection_transfer')=(derived_from_observation_id IS NOT NULL)),"
           'CHECK((legacy_owner IS NULL AND legacy_id IS NULL) OR (legacy_owner IS NOT NULL AND legacy_id IS NOT NULL)), '
           "CHECK(field_scope NOT IN ('legacy_item','legacy_recording') OR (origin LIKE 'legacy_%' AND legacy_owner IS NOT NULL)),"
           "CHECK(entry_id IS NOT NULL OR (field_scope='legacy_recording' AND origin IN ('legacy_attempt','legacy_state'))),"
           "CHECK(field_scope<>'legacy_recording' OR (entry_id IS NULL AND recording_id IS NOT NULL)),"
           "CHECK(recording_id IS NOT NULL OR field_scope='legacy_item'),"
           "CHECK((field IN ('mb_year','label') AND field_scope='release') OR "
           '(field IN ' + _in(RECORDING) + " AND field_scope='recording') OR "
           '(field IN ' + _in(tuple(field for field in ITEM if field not in ('mb_year', 'label')))
           + " AND field_scope='item') OR (field IN ('loudness','dynamic_complexity') AND field_scope='recording') "
           "OR field_scope IN ('legacy_item','legacy_recording')),"
           "CHECK((value_kind='null' AND text_value IS NULL AND number_value IS NULL AND genre_key IS NULL AND unit IS NULL AND resolution_reason IS NULL) OR "
           "(value_kind='blank' AND text_value IS NOT NULL AND text_value='' AND number_value IS NULL AND genre_key IS NULL AND unit IS NULL AND resolution_reason IS NULL) OR "
           "(value_kind='withdrawn' AND origin IN ('user','legacy_choice') AND delivery_id IS NULL AND text_value IS NULL AND "
           'number_value IS NULL AND genre_key IS NULL AND unit IS NULL AND resolution_reason IS NULL) OR '
           "(value_kind='unresolved' AND legacy_owner IS NOT NULL AND delivery_id IS NOT NULL AND text_value IS NULL AND "
           'number_value IS NULL AND genre_key IS NULL AND unit IS NULL AND resolution_reason IS NOT NULL) OR '
           "(value_kind='value' AND ((field IN " + _in(TEXT)
           + ' AND text_value IS NOT NULL AND length(text_value)>0 AND number_value IS NULL AND unit IS NULL AND '
           "((field='genre' AND ((genre_key IS NOT NULL AND resolution_reason IS NULL) OR "
           "(genre_key IS NULL AND resolution_reason IS NOT NULL AND resolution_reason IN ('unknown_genre','ambiguous_genre')))) OR "
           "(field<>'genre' AND genre_key IS NULL AND resolution_reason IS NULL))) OR "
           '(number_value IS NOT NULL AND text_value IS NULL AND genre_key IS NULL AND resolution_reason IS NULL AND '
           'unit IS NOT NULL AND ' + _numeric() + ')))))')
    for name, columns in (('entry', 'entry_id,field,id'), ('recording', 'recording_id,field,id'),
                          ('delivery', 'delivery_id,id'), ('genre', 'genre_key,id'), ('derived', 'derived_from_observation_id')):
        yield f'CREATE INDEX ix_metadata_evidence_{name} ON metadata_observations({columns})'
    yield ('CREATE UNIQUE INDEX ix_metadata_observation_legacy ON metadata_observations(origin,legacy_owner,legacy_id,legacy_position) '
           'WHERE legacy_owner IS NOT NULL')
    yield ('CREATE UNIQUE INDEX ix_metadata_observation_provider_field ON metadata_observations(delivery_id,field,field_scope) '
           "WHERE origin='provider'")
    yield ('CREATE TRIGGER immutable_metadata_observation BEFORE UPDATE ON metadata_observations '
           "BEGIN SELECT RAISE(ABORT,'Metadata evidence is immutable'); END")
    yield ('CREATE TRIGGER retained_metadata_user_history BEFORE DELETE ON metadata_observations '
           "WHEN OLD.origin IN ('user','legacy_choice') BEGIN SELECT RAISE(ABORT,'Metadata choice history is retained'); END")
    yield ('CREATE TRIGGER owned_metadata_observation_delivery BEFORE INSERT ON metadata_observations '
           "WHEN (NEW.origin NOT LIKE 'legacy_%' AND NEW.delivery_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_deliveries d "
           'WHERE d.id=NEW.delivery_id AND d.input_entry_id=NEW.entry_id AND d.input_recording_id=NEW.recording_id '
           'AND d.input_video_id IS NEW.input_video_id)) OR '
           "(NEW.origin LIKE 'legacy_%' AND NOT EXISTS(SELECT 1 FROM metadata_legacy_records r "
           'WHERE r.legacy_owner=NEW.legacy_owner AND r.legacy_id=NEW.legacy_id '
           'AND r.delivery_id=NEW.delivery_id AND r.entry_id IS NEW.entry_id '
           'AND r.recording_id IS NEW.recording_id AND r.input_video_id IS NEW.input_video_id)) '
           "BEGIN SELECT RAISE(ABORT,'Metadata evidence has a different captured input'); END")
    yield ('CREATE TRIGGER current_metadata_decision_subject BEFORE INSERT ON metadata_observations '
           "WHEN NEW.origin IN ('user','selection_snapshot','selection_transfer') AND NEW.delivery_id IS NULL "
           'AND NOT EXISTS(SELECT 1 FROM catalog_entries e WHERE e.id=NEW.entry_id '
           'AND e.recording_id=NEW.recording_id AND e.video_id IS NEW.input_video_id) '
           "BEGIN SELECT RAISE(ABORT,'Metadata decision input changed before capture'); END")
    yield ('CREATE TRIGGER owned_metadata_selection_transfer BEFORE INSERT ON metadata_observations '
           "WHEN NEW.origin='selection_transfer' AND NOT EXISTS(SELECT 1 FROM metadata_observations o "
           'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=NEW.derived_from_observation_id '
           "AND NEW.field_scope='recording' AND o.field_scope IN ('recording','legacy_recording') "
           'AND o.field=NEW.field AND o.field IN ' + _in(RECORDING) + ' '
           "AND o.origin NOT IN ('user','legacy_choice') AND o.value_kind=NEW.value_kind "
           'AND o.text_value IS NEW.text_value AND o.number_value IS NEW.number_value '
           'AND o.genre_key IS NEW.genre_key AND o.unit IS NEW.unit AND o.resolution_reason IS NEW.resolution_reason '
           'AND COALESCE(d.provider,o.legacy_source) IS NEW.legacy_source) '
           "BEGIN SELECT RAISE(ABORT,'Transferred metadata differs from its retained evidence'); END")
    for table, subject, fields in (('recording_metadata_selections', 'recording_id', RECORDING),
                                    ('entry_metadata_selections', 'entry_id', ITEM)):
        owner = 'recordings' if subject == 'recording_id' else 'catalog_entries'
        yield (f'CREATE TABLE {table} ({subject} INTEGER NOT NULL REFERENCES {owner}(id),'
               'field TEXT NOT NULL CHECK(field IN ' + _in(fields) + '),'
               'observation_id INTEGER REFERENCES metadata_observations(id),'
               "attribution_status TEXT NOT NULL CHECK(attribution_status IN ('evidenced','legacy_unresolved')),"
               'legacy_source TEXT,selected_at REAL ' + _clock('selected_at') + ','
               "CHECK((attribution_status='evidenced' AND observation_id IS NOT NULL AND legacy_source IS NULL) OR "
               "(attribution_status='legacy_unresolved' AND observation_id IS NULL AND legacy_source IS NOT NULL)),"
               f'PRIMARY KEY({subject},field)) WITHOUT ROWID')
        yield f'CREATE INDEX ix_{table}_observation ON {table}(observation_id) WHERE observation_id IS NOT NULL'
        for event in ('INSERT', 'UPDATE'):
            condition = "o.field_scope='recording'" if subject == 'recording_id' else "o.field_scope IN ('item','release')"
            yield (f'CREATE TRIGGER owned_{table}_{event.lower()} BEFORE {event} ON {table} '
                   'WHEN NEW.observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_observations o '
                   f'WHERE o.id=NEW.observation_id AND o.{subject}=NEW.{subject} AND o.field=NEW.field AND {condition} '
                   "AND o.value_kind IN ('value','null','blank') AND o.origin NOT IN ('user','legacy_choice')) "
                   "BEGIN SELECT RAISE(ABORT,'Metadata selection evidence has a different subject, field or scope'); END")
    yield ('CREATE TABLE metadata_choice_heads (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),'
           'field TEXT NOT NULL CHECK(field IN ' + _in((*RECORDING, *ITEM)) + '),'
           'observation_id INTEGER NOT NULL REFERENCES metadata_observations(id),PRIMARY KEY(entry_id,field)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_metadata_choice_observation ON metadata_choice_heads(observation_id)'
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER owned_metadata_choice_{event.lower()} BEFORE {event} ON metadata_choice_heads '
               'WHEN NOT EXISTS(SELECT 1 FROM metadata_observations o WHERE o.id=NEW.observation_id '
               'AND o.entry_id=NEW.entry_id AND o.field=NEW.field '
               "AND o.origin IN ('user','legacy_choice','selection_snapshot') AND o.value_kind<>'unresolved') "
               "BEGIN SELECT RAISE(ABORT,'Metadata choice requires its actual retained decision'); END")
    yield ('CREATE TABLE metadata_conflicts (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'entry_id INTEGER REFERENCES catalog_entries(id),recording_id INTEGER REFERENCES recordings(id),'
           "field_scope TEXT NOT NULL CHECK(field_scope IN ('recording','release','item','legacy_item')),"
           'field TEXT NOT NULL CHECK(field IN ' + _in((*RECORDING, *ITEM)) + '),'
           "status TEXT NOT NULL CHECK(status IN ('unresolved','user_resolved','provider_converged')),"
           'selected_observation_id INTEGER REFERENCES metadata_observations(id),updated_at REAL ' + _clock('updated_at') + ','
           'legacy_record_id INTEGER UNIQUE,legacy_resolution_reason TEXT '
           'CHECK(legacy_resolution_reason IS NULL OR length(legacy_resolution_reason)>0),'
           'CHECK((entry_id IS NULL)<>(recording_id IS NULL)),'
           "CHECK((recording_id IS NOT NULL AND field_scope='recording' AND field IN " + _in(RECORDING) + ') OR '
           "(entry_id IS NOT NULL AND field_scope='legacy_item' AND legacy_record_id IS NOT NULL) OR "
           "(entry_id IS NOT NULL AND field_scope='release' AND field IN ('mb_year','label')) OR "
           "(entry_id IS NOT NULL AND field_scope='item' AND field IN "
           + _in(tuple(field for field in ITEM if field not in ('mb_year', 'label'))) + ')),'
           "CHECK(status<>'user_resolved' OR selected_observation_id IS NOT NULL OR "
           '(legacy_record_id IS NOT NULL AND legacy_resolution_reason IS NOT NULL)))')
    yield ('CREATE TRIGGER owned_legacy_metadata_conflict BEFORE INSERT ON metadata_conflicts '
           'WHEN NEW.legacy_record_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_legacy_records r '
           "WHERE r.legacy_owner='enrichment_conflict' AND r.legacy_id=NEW.legacy_record_id "
           'AND ((NEW.entry_id IS NOT NULL AND r.entry_id=NEW.entry_id) OR '
           '(NEW.recording_id IS NOT NULL AND r.recording_id=NEW.recording_id))) '
           "BEGIN SELECT RAISE(ABORT,'Legacy metadata conflict requires its retained original subject'); END")
    yield 'CREATE UNIQUE INDEX ix_metadata_conflict_entry ON metadata_conflicts(entry_id,field) WHERE entry_id IS NOT NULL'
    yield 'CREATE UNIQUE INDEX ix_metadata_conflict_recording ON metadata_conflicts(recording_id,field) WHERE recording_id IS NOT NULL'
    yield ('CREATE INDEX ix_metadata_conflict_selection ON metadata_conflicts(selected_observation_id) '
           'WHERE selected_observation_id IS NOT NULL')
    yield ('CREATE TABLE metadata_conflict_candidates (conflict_id INTEGER NOT NULL REFERENCES metadata_conflicts(id),'
           'observation_id INTEGER NOT NULL REFERENCES metadata_observations(id),'
           "position INTEGER NOT NULL DEFAULT 0 CHECK(typeof(position)='integer' AND position>=0),"
           'PRIMARY KEY(conflict_id,observation_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_metadata_candidate_observation ON metadata_conflict_candidates(observation_id,conflict_id)'
    # An old entry-local conflict can receive newly captured evidence for that
    # same entry without claiming that its original unknown scope was shared.
    compatible = ('o.field=c.field AND ((c.entry_id IS NOT NULL AND o.entry_id=c.entry_id '
                  "AND (o.field_scope=c.field_scope OR c.field_scope='legacy_item')) OR (c.recording_id IS NOT NULL "
                  "AND o.recording_id=c.recording_id AND o.field_scope='recording'))")
    selected_compatible = ('(' + compatible + " OR (c.field_scope='legacy_item' AND o.entry_id=c.entry_id "
                          "AND o.field=c.field AND o.origin IN ('user','legacy_choice')))")
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER owned_metadata_conflict_candidate_{event.lower()} BEFORE {event} ON metadata_conflict_candidates '
               'WHEN NOT EXISTS(SELECT 1 FROM metadata_observations o JOIN metadata_conflicts c ON c.id=NEW.conflict_id '
               'WHERE o.id=NEW.observation_id AND ' + compatible + " AND o.value_kind<>'withdrawn') "
               "BEGIN SELECT RAISE(ABORT,'Metadata conflict candidate has a different subject, field or scope'); END")
        yield (f'CREATE TRIGGER owned_metadata_conflict_selection_{event.lower()} BEFORE {event} ON metadata_conflicts '
               'WHEN NEW.selected_observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_observations o '
               'WHERE o.id=NEW.selected_observation_id AND '
               + selected_compatible.replace('c.entry_id', 'NEW.entry_id').replace('c.recording_id', 'NEW.recording_id')
                                    .replace('c.field_scope', 'NEW.field_scope').replace('c.field', 'NEW.field')
               + " AND o.value_kind IN ('value','null','blank')) "
               "BEGIN SELECT RAISE(ABORT,'Metadata conflict selection has a different subject, field or scope'); END")
    # Conflict subjects are identities, not mutable resolution state. A changed
    # catalog association requires deliberately moving/reopening its evidence.
    yield ('CREATE TRIGGER immutable_metadata_conflict_subject BEFORE UPDATE OF id,entry_id,recording_id,field_scope,field,legacy_record_id ON metadata_conflicts '
           "BEGIN SELECT RAISE(ABORT,'Metadata conflict subject is immutable'); END")
    yield ('CREATE TABLE provider_attempt_fields (attempt_id INTEGER NOT NULL REFERENCES provider_attempts(id),'
           'field TEXT NOT NULL CHECK(field IN ' + _in(ATTEMPT_FIELDS)
           + '),observation_id INTEGER REFERENCES metadata_observations(id),'
           'CHECK(field NOT IN ' + _in((*RECORDING, *ITEM, 'year')) + ' OR observation_id IS NOT NULL),'
           'PRIMARY KEY(attempt_id,field)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_provider_attempt_field ON provider_attempt_fields(field,attempt_id)'
    yield 'CREATE INDEX ix_provider_attempt_field_observation ON provider_attempt_fields(observation_id) WHERE observation_id IS NOT NULL'
    yield ('CREATE TABLE provider_state_fields (recording_id INTEGER NOT NULL,provider TEXT NOT NULL,'
           'field TEXT NOT NULL CHECK(field IN ' + _in(ATTEMPT_FIELDS) + '),'
           'delivery_id INTEGER NOT NULL REFERENCES metadata_deliveries(id),'
           'observation_id INTEGER REFERENCES metadata_observations(id),'
           'CHECK(field NOT IN ' + _in((*RECORDING, *ITEM, 'year')) + ' OR observation_id IS NOT NULL),'
           'FOREIGN KEY(recording_id,provider) REFERENCES enrichment_provider_state(recording_id,provider),'
           'PRIMARY KEY(recording_id,provider,field)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_provider_state_field_observation ON provider_state_fields(observation_id) WHERE observation_id IS NOT NULL'
    yield 'CREATE INDEX ix_provider_state_field_delivery ON provider_state_fields(delivery_id)'
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER owned_provider_attempt_field_{event.lower()} BEFORE {event} ON provider_attempt_fields '
               'WHEN NEW.observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_observations o '
               'JOIN provider_attempts a ON a.id=NEW.attempt_id WHERE o.id=NEW.observation_id '
               "AND o.field=CASE NEW.field WHEN 'year' THEN 'mb_year' ELSE NEW.field END "
               'AND o.delivery_id=a.delivery_id AND o.recording_id=a.recording_id) '
               "BEGIN SELECT RAISE(ABORT,'Attempt field has a different subject, delivery or field'); END")
        yield (f'CREATE TRIGGER owned_provider_state_field_{event.lower()} BEFORE {event} ON provider_state_fields '
               'WHEN NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=NEW.delivery_id AND d.provider=NEW.provider '
               'AND (d.input_recording_id=NEW.recording_id OR EXISTS(SELECT 1 FROM metadata_legacy_records r '
               "WHERE r.delivery_id=d.id AND r.legacy_owner IN ('provider_attempts','enrichment_provider_state') "
               'AND r.recording_id=NEW.recording_id))) OR (NEW.observation_id IS NOT NULL AND NOT EXISTS('
               'SELECT 1 FROM metadata_observations o WHERE o.id=NEW.observation_id '
               'AND o.delivery_id=NEW.delivery_id AND o.recording_id=NEW.recording_id '
               "AND o.field=CASE NEW.field WHEN 'year' THEN 'mb_year' ELSE NEW.field END)) "
               "BEGIN SELECT RAISE(ABORT,'Provider state field has a different subject, delivery or field'); END")
    yield ('CREATE TABLE metadata_hints (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),'
           'field TEXT NOT NULL CHECK(field IN ' + _in((*RECORDING, *ITEM)) + '),'
           'source TEXT NOT NULL CHECK(length(source)>0),observation_id INTEGER NOT NULL REFERENCES metadata_observations(id),'
           "active INTEGER NOT NULL DEFAULT 1 CHECK(typeof(active)='integer' AND active IN (0,1)),"
           'PRIMARY KEY(entry_id,field,source)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_metadata_hint_observation ON metadata_hints(observation_id)'
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER owned_metadata_hint_{event.lower()} BEFORE {event} ON metadata_hints '
               'WHEN NOT EXISTS(SELECT 1 FROM metadata_observations o WHERE o.id=NEW.observation_id '
               'AND o.entry_id=NEW.entry_id AND o.field=NEW.field) '
               "BEGIN SELECT RAISE(ABORT,'Metadata hint has a different entry or field'); END")
    yield 'CREATE UNIQUE INDEX ix_provider_attempt_delivery ON provider_attempts(delivery_id) WHERE delivery_id IS NOT NULL'
    yield 'CREATE INDEX ix_recording_identifier_delivery ON recording_identifiers(delivery_id)'
    yield ('CREATE UNIQUE INDEX ix_recording_identifier_delivery_value ON recording_identifiers(delivery_id,scheme,entity_type,value) '
           'WHERE delivery_id IS NOT NULL')
    yield 'CREATE INDEX ix_recording_identifier_observation ON recording_identifiers(observation_id) WHERE observation_id IS NOT NULL'
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER owned_recording_identifier_delivery_{event.lower()} BEFORE {event} ON recording_identifiers '
               'WHEN NEW.delivery_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_deliveries d '
               'WHERE d.id=NEW.delivery_id AND d.provider=NEW.source '
               'AND ((d.input_recording_id=NEW.recording_id AND d.input_video_id IS NEW.input_video_id) OR '
               'EXISTS(SELECT 1 FROM metadata_legacy_records r WHERE r.delivery_id=d.id '
               "AND r.legacy_owner='recording_identifiers' AND r.recording_id=NEW.recording_id "
               'AND r.input_video_id IS NEW.input_video_id))) '
               "BEGIN SELECT RAISE(ABORT,'Identifier delivery has a different provider or captured subject'); END")
        yield (f'CREATE TRIGGER owned_recording_identifier_observation_{event.lower()} BEFORE {event} ON recording_identifiers '
               'WHEN NEW.observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_observations o '
               "WHERE o.id=NEW.observation_id AND NEW.scheme='musicbrainz' AND NEW.entity_type='recording' "
               "AND o.field='mb_recording_id' AND o.value_kind='value' AND o.text_value=NEW.value "
               'AND o.recording_id=NEW.recording_id AND o.delivery_id=NEW.delivery_id) '
               "BEGIN SELECT RAISE(ABORT,'Identifier observation has a different value, scheme or subject'); END")


def apply(conn, context):
    for table, columns in ADDED_COLUMNS.items():
        for column in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column}')
    for sql in statements():
        conn.execute(sql)


def upgrade(conn, context):
    apply(conn, context)
    convert_legacy(conn)


def projections():
    """Read-only published interfaces; all mutations use their concrete owners."""
    scalar = 'CASE WHEN o.number_value IS NOT NULL THEN o.number_value ELSE o.text_value END'
    yield ('CREATE VIEW entry_field_choices AS SELECT h.entry_id,h.field,json_quote(' + scalar + ') AS value_json,'
           'o.legacy_source AS source FROM metadata_choice_heads h JOIN metadata_observations o ON o.id=h.observation_id '
           "WHERE o.value_kind<>'withdrawn'")
    yield ('CREATE VIEW metadata_decisions AS SELECT o.id,o.entry_id,o.field,json_quote(' + scalar + ') AS value_json,'
           'o.legacy_source AS source,o.observed_at AS chosen_at,o.legacy_reference AS legacy_ref '
           "FROM metadata_observations o WHERE o.origin IN ('user','legacy_choice') AND o.value_kind<>'withdrawn'")
    yield ('CREATE VIEW catalog_field_hints AS SELECT h.entry_id,h.field,h.source,json_quote(' + scalar + ') AS value_json,'
           'h.active FROM metadata_hints h JOIN metadata_observations o ON o.id=h.observation_id')
    for old, new, subject in (('recording_field_sources', 'recording_metadata_selections', 'recording_id'),
                               ('item_field_sources', 'entry_metadata_selections', 'entry_id')):
        yield (f'CREATE VIEW {old} AS SELECT s.{subject},s.field,COALESCE(d.provider,o.legacy_source,s.legacy_source) AS source,'
               f's.selected_at FROM {new} s LEFT JOIN metadata_observations o ON o.id=s.observation_id '
               'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id')
    yield ('CREATE VIEW enrichment_log AS SELECT o.id,o.entry_id AS track_id,o.source_run_id AS run_id,'
           "COALESCE(o.legacy_source,d.provider) AS provider,CASE o.field WHEN 'mb_year' THEN 'year' ELSE o.field END AS field,"
           'CAST(' + scalar + ' AS TEXT) AS value,o.observed_at AS created_at,o.recording_id,o.input_video_id,o.field_scope '
           'FROM metadata_observations o LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id '
           "WHERE o.origin IN ('provider','legacy_log')")
    yield ('CREATE VIEW enrichment_conflict AS SELECT c.id,e.id AS track_id,'
           "CASE c.field WHEN 'mb_year' THEN 'year' ELSE c.field END AS field,"
           "(SELECT json_group_array(json(candidate)) FROM (SELECT json_object('provider',COALESCE(o.legacy_source,d.provider),"
           "'value'," + scalar + ') AS candidate FROM metadata_conflict_candidates p '
           'JOIN metadata_observations o ON o.id=p.observation_id LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id '
           'WHERE p.conflict_id=c.id ORDER BY p.position,o.id)) AS candidates,'
           "CASE c.status WHEN 'user_resolved' THEN 1 WHEN 'provider_converged' THEN 2 ELSE 0 END AS resolved,"
           'CAST(CASE WHEN chosen.number_value IS NOT NULL THEN chosen.number_value ELSE chosen.text_value END AS TEXT) AS resolved_value,'
           'c.updated_at,COALESCE(c.recording_id,r.recording_id) AS recording_id,'
           'r.input_video_id,c.field_scope FROM metadata_conflicts c '
           'JOIN catalog_entries e ON e.id=c.entry_id OR e.recording_id=c.recording_id '
           'LEFT JOIN metadata_observations chosen ON chosen.id=c.selected_observation_id '
           "LEFT JOIN metadata_legacy_records r ON r.legacy_owner='enrichment_conflict' AND r.legacy_id=c.legacy_record_id")


def choice_triggers():
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
        changes = ('OLD.entry_id IS NOT NEW.entry_id', 'OLD.field IS NOT NEW.field')
        changes += tuple('(SELECT ' + column + ' FROM metadata_observations WHERE id=OLD.observation_id) IS NOT '
                         '(SELECT ' + column + ' FROM metadata_observations WHERE id=NEW.observation_id)'
                         for column in ('value_kind', 'text_value', 'number_value'))
        when = ' WHEN ' + ' OR '.join(changes) if event == 'UPDATE' else ''
        yield (f'CREATE TRIGGER rec_capture_metadata_choice_heads_{event.lower()} AFTER {event} '
               f'ON metadata_choice_heads{when} BEGIN INSERT INTO rec_change_queue(kind,item) '
               "VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END")
        writes = []
        for side in sides:
            value = '(SELECT r.identity_key FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id '
            value += f'WHERE e.id={side}.entry_id)'
            writes.append('INSERT INTO rec_content_pending(identity_key) '
                          f'SELECT {value} WHERE {value} IS NOT NULL '
                          'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;')
        yield (f'CREATE TRIGGER rec_content_metadata_choice_heads_{event.lower()} AFTER {event} '
               f'ON metadata_choice_heads{when} BEGIN ' + ''.join(writes) + ' END')


def _catalog_digest(conn):
    import hashlib
    from ..activity import check_cancellable_work
    from ..evidence_ledger import canonical
    digest = hashlib.sha256()
    for row in conn.execute('SELECT * FROM catalog_tracks ORDER BY id'):
        check_cancellable_work()
        digest.update(canonical(tuple(row)))
        digest.update(b'\n')
    return digest.digest()


def convert_legacy(conn):
    """Verify retained originals/current values before retiring duplicate owners."""
    import json
    from .. import metadata_legacy, metadata_native
    from ..activity import check_cancellable_work
    if not conn.in_transaction:
        raise RuntimeError('Metadata conversion requires an outer migration transaction')
    before = _catalog_digest(conn)
    counts = metadata_legacy.backfill_observations(conn)
    metadata_legacy.backfill_selections(conn)
    counts['enrichment_conflict'] = metadata_legacy.backfill_conflicts(conn)
    counts.update(metadata_legacy.backfill_operational(conn))
    counts['recording_identifiers'] = metadata_legacy.backfill_identifiers(conn)
    metadata_legacy.backfill_hints(conn)
    counts['catalog_field_hints'] = conn.execute('SELECT count(*) FROM catalog_field_hints').fetchone()[0]
    for owner, count in counts.items():
        captured = conn.execute('SELECT count(*) FROM metadata_legacy_records WHERE legacy_owner=?', (owner,)).fetchone()[0]
        if captured != count:
            raise MigrationError(f'Metadata original retention is incomplete: {owner}')
    for key, raw in conn.execute('SELECT source_key,payload FROM rec_musical_sources'):
        check_cancellable_work()
        if metadata_native.source_values(conn, key) != json.loads(raw):
            raise MigrationError(f'Metadata source projection differs from its retained evidence: {key}')
    retire_legacy_owners(conn)
    if _catalog_digest(conn) != before:
        raise MigrationError('Metadata conversion changed current catalog values')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise MigrationError('Metadata conversion has an invalid owner reference')
    return counts


def retire_preparation_capture(conn):
    """Detach validated dependency programs before their input owners retire."""
    from ..preparation.metadata_evidence import JOB
    from ..preparation.metadata_conflicts import DEFINITION
    from .current import _tokens
    from .v103_entity_names import transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for job in (JOB, DEFINITION):
        for name, sql in job.definitions(conn):
            if not name.startswith('prepare_'+job.name+'_dependency_'):
                continue
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is not None:
                if _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
                    raise MigrationError('Incompatible retired metadata capture: '+name)
                conn.execute('DROP TRIGGER '+name)


def retire_legacy_owners(conn):
    """Called only after all captured rows and current projections are checked."""
    retire_preparation_capture(conn)
    conn.execute('DROP TRIGGER musical_observation_insert')
    for table in ('entry_field_choices', 'metadata_decisions', 'catalog_field_hints',
                  'recording_field_sources', 'item_field_sources', 'enrichment_log', 'enrichment_conflict', 'metadata_findings'):
        conn.execute(f'DROP TABLE {table}')
    for sql in projections():
        conn.execute(sql)
    for sql in choice_triggers():
        conn.execute(sql)
    conn.execute('ALTER TABLE rec_musical_sources DROP COLUMN payload')
    for table in ('provider_attempts', 'enrichment_provider_state'):
        conn.execute(f'ALTER TABLE {table} DROP COLUMN fields_json')


def validate(conn):
    from .current import _tokens
    for sql in statements():
        words = sql.split()
        name = words[3] if words[1] == 'UNIQUE' else words[2]
        actual = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible finite metadata evidence: {name}')
