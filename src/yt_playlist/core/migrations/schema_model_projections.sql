CREATE TABLE model_pair_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL CHECK(typeof(run_id)='integer' AND run_id>0),
    run_ref INTEGER REFERENCES rec_model_selection_runs(id),
    run_resolution TEXT,
    arm TEXT NOT NULL CHECK(arm IN ('incumbent','challenger')),
    format TEXT NOT NULL CHECK(format IN ('mixes','albums','radio')),
    generation_id INTEGER REFERENCES recommendation_generations(id),
    encoding_model TEXT CHECK(encoding_model IS NULL OR length(encoding_model) BETWEEN 1 AND 4096),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    output_digest TEXT NOT NULL CHECK(length(output_digest)=64),
    part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'building' CHECK(status IN ('building','published','retiring')),
    CHECK((run_ref IS NOT NULL AND run_ref=run_id AND run_resolution IS NULL) OR
          (run_ref IS NULL AND run_resolution IS NOT NULL AND run_resolution='missing_run'))
);
CREATE TABLE model_pair_heads (
    run_id INTEGER NOT NULL,
    arm TEXT NOT NULL CHECK(arm IN ('incumbent','challenger')),
    format TEXT NOT NULL CHECK(format IN ('mixes','albums','radio')),
    publication_id INTEGER NOT NULL UNIQUE REFERENCES model_pair_publications(id),
    PRIMARY KEY(run_id,arm,format)
) WITHOUT ROWID;
CREATE TABLE model_pair_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES model_pair_publications(id),
    surface TEXT NOT NULL,
    chunk_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
    byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576),
    UNIQUE(publication_id,chunk_key)
);
CREATE TABLE model_pair_policies (
    publication_id INTEGER NOT NULL REFERENCES model_pair_publications(id),
    raw_version TEXT NOT NULL,
    policy_version TEXT REFERENCES recommendation_policies(version),
    resolution_reason TEXT,
    PRIMARY KEY(publication_id,raw_version),
    CHECK((policy_version IS NOT NULL AND policy_version=raw_version AND resolution_reason IS NULL) OR
          (policy_version IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_policy'))
) WITHOUT ROWID;
CREATE INDEX ix_model_pair_policies ON model_pair_policies(policy_version);
CREATE TABLE model_pair_contexts (
    publication_id INTEGER NOT NULL REFERENCES model_pair_publications(id),
    field TEXT NOT NULL CHECK(field IN ('context','choice_context','album_choice_context')),
    artifact TEXT NOT NULL CHECK(length(artifact) BETWEEN 1 AND 1024),
    PRIMARY KEY(publication_id,field)
) WITHOUT ROWID;
CREATE TABLE model_pair_modes (
    publication_id INTEGER NOT NULL REFERENCES model_pair_publications(id),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 511),
    mode_id INTEGER NOT NULL CHECK(typeof(mode_id)='integer' AND mode_id>0),
    mode_ref INTEGER REFERENCES taste_modes(mode_id),
    resolution_reason TEXT,
    space TEXT NOT NULL,
    centroid BLOB NOT NULL CHECK(typeof(centroid)='blob' AND length(centroid) BETWEEN 8 AND 32768 AND length(centroid)%8=0),
    PRIMARY KEY(publication_id,position),
    UNIQUE(publication_id,mode_id),
    CHECK((mode_ref IS NOT NULL AND mode_ref=mode_id AND resolution_reason IS NULL) OR
          (mode_ref IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_mode'))
) WITHOUT ROWID;
CREATE TABLE model_pair_group_roots (
    publication_id INTEGER NOT NULL REFERENCES model_pair_publications(id),
    field TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind='comparison'),
    external_key TEXT NOT NULL,
    PRIMARY KEY(publication_id,field),
    FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)
) WITHOUT ROWID;
CREATE TABLE model_pair_recommendation_roots (
    publication_id INTEGER NOT NULL REFERENCES model_pair_publications(id),
    field TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind='comparison'),
    external_key TEXT NOT NULL,
    generation_id INTEGER NOT NULL,
    PRIMARY KEY(publication_id,field,generation_id),
    FOREIGN KEY(kind,external_key,generation_id) REFERENCES recommendation_generation_refs(kind,external_key,generation_id)
) WITHOUT ROWID;
CREATE TRIGGER model_pair_head_insert BEFORE INSERT ON model_pair_heads BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM model_pair_publications p WHERE p.id=NEW.publication_id
      AND p.run_id=NEW.run_id AND p.arm=NEW.arm AND p.format=NEW.format AND p.status='published')
      THEN RAISE(ABORT,'Comparison head requires matching published output') END;
END;
CREATE TRIGGER model_pair_head_update BEFORE UPDATE ON model_pair_heads BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM model_pair_publications p WHERE p.id=NEW.publication_id
      AND p.run_id=NEW.run_id AND p.arm=NEW.arm AND p.format=NEW.format AND p.status='published')
      THEN RAISE(ABORT,'Comparison head requires matching published output') END;
END;
CREATE TRIGGER model_pair_seal BEFORE UPDATE OF status ON model_pair_publications
WHEN NEW.status='published' BEGIN
    SELECT CASE WHEN OLD.status<>'building' OR NEW.part_count<>(SELECT count(*) FROM model_pair_files WHERE publication_id=NEW.id)
      OR (NEW.generation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM recommendation_generations g
          WHERE g.id=NEW.generation_id AND g.status='published'))
      THEN RAISE(ABORT,'Comparison publication is incomplete') END;
END;
CREATE TRIGGER model_pair_retire BEFORE UPDATE OF status ON model_pair_publications
WHEN NEW.status='retiring' AND EXISTS(SELECT 1 FROM model_pair_heads WHERE publication_id=OLD.id) BEGIN
    SELECT RAISE(ABORT,'Comparison output still has a serving head');
END;
CREATE TRIGGER model_pair_publication_immutable BEFORE UPDATE ON model_pair_publications
WHEN OLD.status<>'building' AND NOT (OLD.status='published' AND NEW.status='retiring'
    AND NEW.id=OLD.id AND NEW.run_id=OLD.run_id AND NEW.run_ref IS OLD.run_ref
    AND NEW.run_resolution IS OLD.run_resolution AND NEW.arm=OLD.arm AND NEW.format=OLD.format
    AND NEW.generation_id IS OLD.generation_id AND NEW.encoding_model IS OLD.encoding_model
    AND NEW.built_at IS OLD.built_at AND NEW.output_digest=OLD.output_digest AND NEW.part_count=OLD.part_count) BEGIN
    SELECT RAISE(ABORT,'Published comparison is immutable');
END;
