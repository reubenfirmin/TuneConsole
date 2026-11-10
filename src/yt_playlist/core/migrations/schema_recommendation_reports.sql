CREATE TABLE recommendation_report_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    surface TEXT NOT NULL CHECK(surface IN ('listening_choices','choice_daily','model_health','model_live_report')),
    status TEXT NOT NULL DEFAULT 'building' CHECK(status IN ('building','published','retiring')),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    observed_at REAL CHECK(observed_at IS NULL OR (typeof(observed_at) IN ('integer','real') AND abs(observed_at)<=1.7976931348623157e308)),
    schema_version INTEGER CHECK(schema_version IS NULL OR (typeof(schema_version)='integer' AND schema_version>=0)),
    watermark TEXT,
    report_key TEXT,
    evidence_generation TEXT,
    inputs_key TEXT,
    next_at REAL CHECK(next_at IS NULL OR (typeof(next_at) IN ('integer','real') AND abs(next_at)<=1.7976931348623157e308)),
    vibe_episodes INTEGER CHECK(vibe_episodes IS NULL OR (typeof(vibe_episodes)='integer' AND vibe_episodes>=0)),
    half_life_days REAL CHECK(half_life_days IS NULL OR (typeof(half_life_days) IN ('integer','real') AND half_life_days>0 AND half_life_days<=1.7976931348623157e308)),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    total_bytes INTEGER NOT NULL CHECK(typeof(total_bytes)='integer' AND total_bytes BETWEEN 1 AND 4194304),
    has_content INTEGER NOT NULL CHECK(typeof(has_content)='integer' AND has_content IN (0,1)),
    part_count INTEGER NOT NULL CHECK(typeof(part_count)='integer' AND part_count BETWEEN 1 AND 64),
    UNIQUE(id,surface)
);
CREATE TABLE recommendation_report_heads (
    surface TEXT PRIMARY KEY NOT NULL,
    publication_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY(publication_id,surface) REFERENCES recommendation_report_publications(id,surface)
) WITHOUT ROWID;
CREATE TABLE recommendation_report_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    publication_id INTEGER NOT NULL,
    surface TEXT NOT NULL,
    chunk_key TEXT NOT NULL CHECK(chunk_key=CAST(CAST(chunk_key AS INTEGER) AS TEXT) AND CAST(chunk_key AS INTEGER) BETWEEN 0 AND 63),
    filename TEXT NOT NULL CHECK(length(filename)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    UNIQUE(publication_id,chunk_key),
    FOREIGN KEY(publication_id,surface) REFERENCES recommendation_report_publications(id,surface)
);
CREATE TABLE choice_format_usage (
    publication_id INTEGER NOT NULL REFERENCES recommendation_report_publications(id),
    format TEXT NOT NULL CHECK(format IN ('mixes','albums','radio')),
    field TEXT NOT NULL CHECK(field IN ('scores','episodes','days')),
    value NOT NULL CHECK(typeof(value) IN ('integer','real') AND value>=0 AND value<=1.7976931348623157e308 AND (field='scores' OR typeof(value)='integer')),
    PRIMARY KEY(publication_id,format,field)
) WITHOUT ROWID;
CREATE TRIGGER recommendation_report_new BEFORE INSERT ON recommendation_report_publications
WHEN NEW.status<>'building' BEGIN SELECT RAISE(ABORT,'Report starts privately'); END;
CREATE TRIGGER recommendation_report_identity BEFORE UPDATE OF id,surface,built_at,observed_at,schema_version,watermark,report_key,evidence_generation,inputs_key,next_at,vibe_episodes,half_life_days,content_digest,total_bytes,has_content,part_count ON recommendation_report_publications
BEGIN SELECT RAISE(ABORT,'Report inputs are immutable'); END;
CREATE TRIGGER recommendation_report_transition BEFORE UPDATE OF status ON recommendation_report_publications
WHEN NOT ((OLD.status='building' AND NEW.status='published'
    AND NEW.part_count=(SELECT count(*) FROM recommendation_report_files WHERE publication_id=OLD.id)
    AND (SELECT min(CAST(chunk_key AS INTEGER)) FROM recommendation_report_files WHERE publication_id=OLD.id)=0
    AND (SELECT max(CAST(chunk_key AS INTEGER)) FROM recommendation_report_files WHERE publication_id=OLD.id)=NEW.part_count-1)
    OR (OLD.status='published' AND NEW.status='retiring' AND NOT EXISTS(SELECT 1 FROM recommendation_report_heads WHERE publication_id=OLD.id)))
BEGIN SELECT RAISE(ABORT,'Report is incomplete or retained'); END;
CREATE TRIGGER recommendation_report_retained BEFORE DELETE ON recommendation_report_publications
WHEN OLD.status<>'retiring' BEGIN SELECT RAISE(ABORT,'Report must retire first'); END;
CREATE TRIGGER recommendation_report_head_insert BEFORE INSERT ON recommendation_report_heads
WHEN NOT EXISTS(SELECT 1 FROM recommendation_report_publications WHERE id=NEW.publication_id AND status='published')
BEGIN SELECT RAISE(ABORT,'Report head requires published output'); END;
CREATE TRIGGER recommendation_report_head_update BEFORE UPDATE ON recommendation_report_heads
WHEN NOT EXISTS(SELECT 1 FROM recommendation_report_publications WHERE id=NEW.publication_id AND status='published')
BEGIN SELECT RAISE(ABORT,'Report head requires published output'); END;
CREATE TRIGGER recommendation_report_file_insert BEFORE INSERT ON recommendation_report_files
WHEN NOT EXISTS(SELECT 1 FROM recommendation_report_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Report files are sealed'); END;
CREATE TRIGGER recommendation_report_file_update BEFORE UPDATE ON recommendation_report_files
WHEN NEW.id IS NOT OLD.id OR NEW.publication_id IS NOT OLD.publication_id OR NEW.surface IS NOT OLD.surface OR NEW.chunk_key IS NOT OLD.chunk_key OR NEW.content_digest IS NOT OLD.content_digest OR NEW.byte_size IS NOT OLD.byte_size
BEGIN SELECT RAISE(ABORT,'Report files are immutable'); END;
CREATE TRIGGER recommendation_report_file_delete BEFORE DELETE ON recommendation_report_files
WHEN NOT EXISTS(SELECT 1 FROM recommendation_report_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Report files are retained'); END;
CREATE TRIGGER choice_format_usage_insert BEFORE INSERT ON choice_format_usage
WHEN NOT EXISTS(SELECT 1 FROM recommendation_report_publications WHERE id=NEW.publication_id AND surface='listening_choices' AND status='building')
BEGIN SELECT RAISE(ABORT,'Format usage belongs to the choice report'); END;
CREATE TRIGGER choice_format_usage_update BEFORE UPDATE ON choice_format_usage
BEGIN SELECT RAISE(ABORT,'Published format usage is immutable'); END;
CREATE TRIGGER choice_format_usage_delete BEFORE DELETE ON choice_format_usage
WHEN NOT EXISTS(SELECT 1 FROM recommendation_report_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Format usage is retained'); END;
CREATE TRIGGER retired_recommendation_report_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface IN ('listening_choices','choice_daily','model_health','model_live_report')
BEGIN SELECT RAISE(ABORT,'Reports have publication owners'); END;
CREATE TRIGGER retired_recommendation_report_update BEFORE UPDATE ON rec_proposals
WHEN NEW.surface IN ('listening_choices','choice_daily','model_health','model_live_report')
BEGIN SELECT RAISE(ABORT,'Reports have publication owners'); END;
