CREATE TABLE choice_learning_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    surface TEXT NOT NULL CHECK(surface IN ('choice_bandit','group_choice_bandit','choice_interests')),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 131071),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    schema_version INTEGER CHECK(schema_version IS NULL OR (typeof(schema_version)='integer' AND schema_version>=0)),
    policy TEXT,
    generation TEXT,
    state_version TEXT,
    at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
    since REAL CHECK(since IS NULL OR (typeof(since) IN ('integer','real') AND abs(since)<=1.7976931348623157e308)),
    expires_at REAL CHECK(expires_at IS NULL OR (typeof(expires_at) IN ('integer','real') AND abs(expires_at)<=1.7976931348623157e308)),
    offered INTEGER CHECK(offered IS NULL OR (typeof(offered)='integer' AND offered>=0)),
    picked INTEGER CHECK(picked IS NULL OR (typeof(picked)='integer' AND picked>=0)),
    watermark TEXT,
    contexts_total INTEGER CHECK(contexts_total IS NULL OR (typeof(contexts_total)='integer' AND contexts_total>=0)),
    interest_version TEXT,
    UNIQUE(id,surface)
);
CREATE TABLE choice_learning_heads (
    surface TEXT PRIMARY KEY NOT NULL,
    publication_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY(publication_id,surface) REFERENCES choice_learning_publications(id,surface)
) WITHOUT ROWID;
CREATE TABLE choice_learning_parameters (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(name IN ('choice','open','start','completion','promotion','early_exit','window_d','match_threshold')),
    value REAL NOT NULL CHECK(typeof(value) IN ('integer','real') AND abs(value)<=1.7976931348623157e308),
    PRIMARY KEY(publication_id,name)
) WITHOUT ROWID;
CREATE TABLE choice_learning_exclusions (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(name IN ('unseen','unmapped_mode','drifted_mode','future_context','missing_context','unmapped_definition','legacy_ambiguous')),
    value INTEGER NOT NULL CHECK(typeof(value)='integer' AND value>=0),
    PRIMARY KEY(publication_id,name)
) WITHOUT ROWID;
CREATE TABLE choice_mode_estimates (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 8191),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 511),
    mode_id CHECK(mode_id IS NULL OR typeof(mode_id) IN ('integer','text')),
    label TEXT,
    positive REAL CHECK(positive IS NULL OR (typeof(positive) IN ('integer','real') AND positive>=0 AND positive<=1.7976931348623157e308)),
    negative REAL CHECK(negative IS NULL OR (typeof(negative) IN ('integer','real') AND negative>=0 AND negative<=1.7976931348623157e308)),
    days INTEGER CHECK(days IS NULL OR (typeof(days)='integer' AND days>=0)),
    menus INTEGER CHECK(menus IS NULL OR (typeof(menus)='integer' AND menus>=0)),
    estimate REAL CHECK(estimate IS NULL OR estimate BETWEEN 0 AND 1),
    low REAL CHECK(low IS NULL OR low BETWEEN 0 AND 1),
    high REAL CHECK(high IS NULL OR high BETWEEN 0 AND 1),
    resolved_mode_id INTEGER REFERENCES taste_modes(mode_id),
    resolution_reason TEXT,
    CHECK((resolved_mode_id IS NOT NULL AND resolved_mode_id=mode_id AND resolution_reason IS NULL) OR (resolved_mode_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_mode')),
    PRIMARY KEY(publication_id,position),
    UNIQUE(publication_id,mode_id)
) WITHOUT ROWID;
CREATE TABLE choice_group_estimates (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 8191),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 8191),
    group_id TEXT,
    version TEXT,
    positive REAL CHECK(positive IS NULL OR (typeof(positive) IN ('integer','real') AND positive>=0 AND positive<=1.7976931348623157e308)),
    negative REAL CHECK(negative IS NULL OR (typeof(negative) IN ('integer','real') AND negative>=0 AND negative<=1.7976931348623157e308)),
    offered INTEGER CHECK(offered IS NULL OR (typeof(offered)='integer' AND offered>=0)),
    picked INTEGER CHECK(picked IS NULL OR (typeof(picked)='integer' AND picked>=0)),
    days INTEGER CHECK(days IS NULL OR (typeof(days)='integer' AND days>=0)),
    shared_strength REAL CHECK(shared_strength IS NULL OR shared_strength BETWEEN 0 AND 1),
    shared_from TEXT,
    alpha REAL CHECK(alpha IS NULL OR (typeof(alpha) IN ('integer','real') AND alpha>0 AND alpha<=1.7976931348623157e308)),
    beta REAL CHECK(beta IS NULL OR (typeof(beta) IN ('integer','real') AND beta>0 AND beta<=1.7976931348623157e308)),
    estimate REAL CHECK(estimate IS NULL OR estimate BETWEEN 0 AND 1),
    PRIMARY KEY(publication_id,position),
    UNIQUE(publication_id,group_id)
) WITHOUT ROWID;
CREATE TABLE choice_group_opportunities (
    publication_id INTEGER NOT NULL,
    estimate_position INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 8191),
    opportunity_id TEXT NOT NULL CHECK(length(opportunity_id)>0),
    PRIMARY KEY(publication_id,estimate_position,position),
    UNIQUE(publication_id,estimate_position,opportunity_id),
    FOREIGN KEY(publication_id,estimate_position) REFERENCES choice_group_estimates(publication_id,position) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE choice_learning_group_roots (
    publication_id INTEGER PRIMARY KEY NOT NULL REFERENCES choice_learning_publications(id),
    kind TEXT NOT NULL CHECK(kind='prepared'),
    external_key TEXT NOT NULL UNIQUE CHECK(external_key='choice-learning:'||publication_id),
    FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)
) WITHOUT ROWID;
CREATE TABLE choice_interest_contexts (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 127),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 8191),
    format TEXT CHECK(format IS NULL OR format IN ('mixes','albums','radio')),
    option_id TEXT,
    mode_id CHECK(mode_id IS NULL OR typeof(mode_id) IN ('integer','text')),
    intent TEXT,
    basis TEXT,
    at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
    episode TEXT,
    opportunity_id TEXT,
    days INTEGER CHECK(days IS NULL OR (typeof(days)='integer' AND days>=0)),
    episodes INTEGER CHECK(episodes IS NULL OR (typeof(episodes)='integer' AND episodes>=0)),
    episode_support REAL CHECK(episode_support IS NULL OR (typeof(episode_support) IN ('integer','real') AND episode_support>=0 AND episode_support<=1.7976931348623157e308)),
    feature_null INTEGER NOT NULL CHECK(typeof(feature_null)='integer' AND feature_null IN (0,1)),
    album_id INTEGER REFERENCES albums(id),
    resolution_reason TEXT,
    CHECK((format='albums' AND ((album_id IS NOT NULL AND resolution_reason IS NULL) OR (album_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_album'))) OR ((format IS NULL OR format<>'albums') AND album_id IS NULL AND resolution_reason IS NULL)),
    PRIMARY KEY(publication_id,position)
) WITHOUT ROWID;
CREATE TABLE choice_interest_features (
    publication_id INTEGER NOT NULL,
    context_position INTEGER NOT NULL,
    dimension INTEGER NOT NULL CHECK(typeof(dimension)='integer' AND dimension BETWEEN 0 AND 8191),
    value REAL NOT NULL CHECK(typeof(value) IN ('integer','real') AND abs(value)<=1.7976931348623157e308),
    PRIMARY KEY(publication_id,context_position,dimension),
    FOREIGN KEY(publication_id,context_position) REFERENCES choice_interest_contexts(publication_id,position) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE choice_interest_tracks (
    publication_id INTEGER NOT NULL,
    context_position INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 99),
    raw_key TEXT NOT NULL CHECK(length(raw_key)>0),
    recording_id INTEGER REFERENCES recordings(id),
    resolution_reason TEXT,
    CHECK((recording_id IS NOT NULL AND resolution_reason IS NULL) OR (recording_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_recording')),
    PRIMARY KEY(publication_id,context_position,position),
    UNIQUE(publication_id,context_position,raw_key),
    FOREIGN KEY(publication_id,context_position) REFERENCES choice_interest_contexts(publication_id,position) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX ix_choice_interest_recording ON choice_interest_tracks(recording_id,publication_id) WHERE recording_id IS NOT NULL;
CREATE TABLE choice_album_interests (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 127),
    browse_id TEXT NOT NULL CHECK(length(browse_id)>0),
    album_id INTEGER REFERENCES albums(id),
    resolution_reason TEXT,
    CHECK((album_id IS NOT NULL AND resolution_reason IS NULL) OR (album_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_album')),
    PRIMARY KEY(publication_id,position),
    UNIQUE(publication_id,browse_id)
) WITHOUT ROWID;
CREATE INDEX ix_choice_interest_album ON choice_album_interests(album_id,publication_id) WHERE album_id IS NOT NULL;
CREATE TABLE choice_album_track_days (
    publication_id INTEGER NOT NULL,
    album_position INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 999),
    raw_key TEXT NOT NULL CHECK(length(raw_key)>0),
    recording_id INTEGER REFERENCES recordings(id),
    days INTEGER NOT NULL CHECK(typeof(days)='integer' AND days>=0),
    resolution_reason TEXT,
    CHECK((recording_id IS NOT NULL AND resolution_reason IS NULL) OR (recording_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_recording')),
    PRIMARY KEY(publication_id,album_position,position),
    UNIQUE(publication_id,album_position,raw_key),
    FOREIGN KEY(publication_id,album_position) REFERENCES choice_album_interests(publication_id,position) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX ix_choice_album_day_recording ON choice_album_track_days(recording_id,publication_id) WHERE recording_id IS NOT NULL;
CREATE TRIGGER retired_choice_learning_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface IN ('choice_bandit','group_choice_bandit','choice_interests')
BEGIN SELECT RAISE(ABORT,'Choice learning has a relational owner'); END;
CREATE TRIGGER retired_choice_learning_update BEFORE UPDATE ON rec_proposals
WHEN NEW.surface IN ('choice_bandit','group_choice_bandit','choice_interests')
BEGIN SELECT RAISE(ABORT,'Choice learning has a relational owner'); END;
