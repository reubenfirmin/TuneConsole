CREATE TABLE home_display_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    surface TEXT NOT NULL CHECK(surface IN ('home_prepared','home_tuned','home_choice','album_prepared','adaptive_group_preview','home_discovery','home_revisit','into_recently','trend_rollups','cleanup')),
    status TEXT NOT NULL DEFAULT 'building' CHECK(status IN ('building','published','retiring')),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    schema_version INTEGER CHECK(schema_version IS NULL OR (typeof(schema_version)='integer' AND schema_version>=0)),
    generation_key TEXT CHECK(generation_key IS NULL OR length(CAST(generation_key AS BLOB)) BETWEEN 1 AND 1024),
    mix_regions INTEGER NOT NULL CHECK(typeof(mix_regions)='integer' AND mix_regions>=0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    total_bytes INTEGER NOT NULL CHECK(typeof(total_bytes)='integer' AND total_bytes BETWEEN 1 AND 8388608),
    part_count INTEGER NOT NULL CHECK(typeof(part_count)='integer' AND part_count BETWEEN 1 AND 128),
    has_content INTEGER NOT NULL CHECK(typeof(has_content)='integer' AND has_content IN (0,1)),
    input_revision INTEGER CHECK(input_revision IS NULL OR (typeof(input_revision)='integer' AND input_revision>=0)),
    revision_reason TEXT CHECK((input_revision IS NULL AND revision_reason IS NOT NULL AND revision_reason IN ('legacy_not_recorded','publisher_not_recorded')) OR (input_revision IS NOT NULL AND revision_reason IS NULL)),
    UNIQUE(id,surface)
);
CREATE TABLE home_display_heads (
    surface TEXT PRIMARY KEY NOT NULL,
    publication_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY(publication_id,surface) REFERENCES home_display_publications(id,surface)
) WITHOUT ROWID;
CREATE TABLE home_display_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    publication_id INTEGER NOT NULL,
    surface TEXT NOT NULL,
    chunk_key TEXT NOT NULL CHECK(chunk_key=CAST(CAST(chunk_key AS INTEGER) AS TEXT) AND CAST(chunk_key AS INTEGER) BETWEEN 0 AND 127),
    filename TEXT NOT NULL CHECK(length(filename)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    UNIQUE(publication_id,chunk_key),
    FOREIGN KEY(publication_id,surface) REFERENCES home_display_publications(id,surface)
);
CREATE TABLE home_display_policies (
    publication_id INTEGER NOT NULL REFERENCES home_display_publications(id),
    raw_key TEXT NOT NULL CHECK(length(raw_key)>0),
    policy_version TEXT REFERENCES recommendation_policies(version),
    resolution_reason TEXT,
    CHECK((policy_version IS NOT NULL AND policy_version=raw_key AND resolution_reason IS NULL) OR (policy_version IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_policy')),
    PRIMARY KEY(publication_id,raw_key)
) WITHOUT ROWID;
CREATE INDEX ix_home_display_policy ON home_display_policies(policy_version,publication_id) WHERE policy_version IS NOT NULL;
CREATE TABLE home_display_group_roots (
    publication_id INTEGER NOT NULL REFERENCES home_display_publications(id),
    field TEXT NOT NULL CHECK(field IN ('context','choice_context','album_choice_context')),
    kind TEXT NOT NULL CHECK(kind='prepared'),
    external_key TEXT NOT NULL CHECK(external_key='home-output:'||publication_id||':'||field),
    PRIMARY KEY(publication_id,field),
    FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key)
) WITHOUT ROWID;
CREATE TABLE home_display_recommendation_roots (
    publication_id INTEGER NOT NULL REFERENCES home_display_publications(id),
    field TEXT NOT NULL CHECK(field IN ('context','choice_context','album_choice_context','recommendations')),
    kind TEXT NOT NULL CHECK(kind='prepared'),
    external_key TEXT NOT NULL CHECK(external_key='home-output:'||publication_id||':'||field),
    generation_id INTEGER NOT NULL,
    PRIMARY KEY(publication_id,field,generation_id),
    FOREIGN KEY(kind,external_key,generation_id) REFERENCES recommendation_generation_refs(kind,external_key,generation_id)
) WITHOUT ROWID;
CREATE TRIGGER home_display_new BEFORE INSERT ON home_display_publications
WHEN NEW.status<>'building' BEGIN SELECT RAISE(ABORT,'Home display starts privately'); END;
CREATE TRIGGER home_display_identity BEFORE UPDATE OF id,surface,built_at,schema_version,generation_key,mix_regions,content_digest,total_bytes,part_count,input_revision,revision_reason,has_content ON home_display_publications
BEGIN SELECT RAISE(ABORT,'Home display inputs are immutable'); END;
CREATE TRIGGER home_display_transition BEFORE UPDATE OF status ON home_display_publications
WHEN NOT ((OLD.status='building' AND NEW.status='published'
    AND NEW.part_count=(SELECT count(*) FROM home_display_files WHERE publication_id=OLD.id)
    AND (SELECT min(CAST(chunk_key AS INTEGER)) FROM home_display_files WHERE publication_id=OLD.id)=0
    AND (SELECT max(CAST(chunk_key AS INTEGER)) FROM home_display_files WHERE publication_id=OLD.id)=NEW.part_count-1)
    OR (OLD.status='published' AND NEW.status='retiring' AND NOT EXISTS(SELECT 1 FROM home_display_heads WHERE publication_id=OLD.id)))
BEGIN SELECT RAISE(ABORT,'Home display is incomplete or retained'); END;
CREATE TRIGGER home_display_retained BEFORE DELETE ON home_display_publications
WHEN OLD.status<>'retiring' BEGIN SELECT RAISE(ABORT,'Home display must retire first'); END;
CREATE TRIGGER home_display_head_insert BEFORE INSERT ON home_display_heads
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=NEW.publication_id AND status='published')
BEGIN SELECT RAISE(ABORT,'Home head requires published display'); END;
CREATE TRIGGER home_display_head_update BEFORE UPDATE ON home_display_heads
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=NEW.publication_id AND status='published')
BEGIN SELECT RAISE(ABORT,'Home head requires published display'); END;
CREATE TRIGGER home_display_file_insert BEFORE INSERT ON home_display_files
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Home display files are sealed'); END;
CREATE TRIGGER home_display_file_update BEFORE UPDATE ON home_display_files
WHEN NEW.id IS NOT OLD.id OR NEW.publication_id IS NOT OLD.publication_id OR NEW.surface IS NOT OLD.surface OR NEW.chunk_key IS NOT OLD.chunk_key OR NEW.content_digest IS NOT OLD.content_digest OR NEW.byte_size IS NOT OLD.byte_size
BEGIN SELECT RAISE(ABORT,'Home display files are immutable'); END;
CREATE TRIGGER home_display_file_delete BEFORE DELETE ON home_display_files
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Home display files are retained'); END;
CREATE TRIGGER home_display_policy_insert BEFORE INSERT ON home_display_policies
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Home display policy references are sealed'); END;
CREATE TRIGGER home_display_policy_update BEFORE UPDATE ON home_display_policies
BEGIN SELECT RAISE(ABORT,'Home display policy references are immutable'); END;
CREATE TRIGGER home_display_policy_delete BEFORE DELETE ON home_display_policies
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Home display policy references are retained'); END;
CREATE TRIGGER home_display_group_insert BEFORE INSERT ON home_display_group_roots
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Home display group references are sealed'); END;
CREATE TRIGGER home_display_group_update BEFORE UPDATE ON home_display_group_roots
BEGIN SELECT RAISE(ABORT,'Home display group references are immutable'); END;
CREATE TRIGGER home_display_group_delete BEFORE DELETE ON home_display_group_roots
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Home display group references are retained'); END;
CREATE TRIGGER home_display_recommendation_insert BEFORE INSERT ON home_display_recommendation_roots
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Home display generation references are sealed'); END;
CREATE TRIGGER home_display_recommendation_update BEFORE UPDATE ON home_display_recommendation_roots
BEGIN SELECT RAISE(ABORT,'Home display generation references are immutable'); END;
CREATE TRIGGER home_display_recommendation_delete BEFORE DELETE ON home_display_recommendation_roots
WHEN NOT EXISTS(SELECT 1 FROM home_display_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Home display generation references are retained'); END;
CREATE TRIGGER retired_home_display_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface IN ('home_prepared','home_tuned','home_choice','album_prepared','adaptive_group_preview','home_discovery','home_revisit','into_recently','trend_rollups','cleanup')
BEGIN SELECT RAISE(ABORT,'Home display has a publication owner'); END;
CREATE TRIGGER retired_home_display_update BEFORE UPDATE ON rec_proposals
WHEN NEW.surface IN ('home_prepared','home_tuned','home_choice','album_prepared','adaptive_group_preview','home_discovery','home_revisit','into_recently','trend_rollups','cleanup')
BEGIN SELECT RAISE(ABORT,'Home display has a publication owner'); END;
