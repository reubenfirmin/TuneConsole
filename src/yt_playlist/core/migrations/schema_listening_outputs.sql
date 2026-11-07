CREATE TABLE listening_output_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    status TEXT NOT NULL DEFAULT 'building' CHECK(status IN ('building','published','retiring')),
    schema_version INTEGER CHECK(schema_version IS NULL OR (typeof(schema_version)='integer' AND schema_version>0)),
    generation_key TEXT CHECK(generation_key IS NULL OR (length(generation_key)=24 AND generation_key NOT GLOB '*[^0-9a-f]*')),
    timezone TEXT CHECK(timezone IS NULL OR length(CAST(timezone AS BLOB)) BETWEEN 1 AND 1024),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    input_revision INTEGER CHECK(input_revision IS NULL OR (typeof(input_revision)='integer' AND input_revision>=0)),
    revision_reason TEXT CHECK((input_revision IS NULL AND revision_reason IS NOT NULL AND revision_reason='legacy_not_recorded') OR (input_revision IS NOT NULL AND revision_reason IS NULL)),
    output_count INTEGER NOT NULL CHECK(typeof(output_count)='integer' AND output_count BETWEEN 0 AND 4098),
    chunk_count INTEGER NOT NULL CHECK(typeof(chunk_count)='integer' AND chunk_count BETWEEN 0 AND 4096)
);
CREATE TABLE listening_output_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    publication_id INTEGER NOT NULL REFERENCES listening_output_publications(id),
    surface TEXT NOT NULL CHECK(surface IN ('listening_analytics','listening_edition','listening_chunk')),
    chunk_key TEXT NOT NULL CHECK((surface='listening_chunk' AND length(chunk_key)=24 AND chunk_key NOT GLOB '*[^0-9a-f]*') OR (surface<>'listening_chunk' AND chunk_key='')),
    filename TEXT NOT NULL CHECK(length(filename)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    UNIQUE(publication_id,surface,chunk_key)
);
CREATE TABLE listening_output_chunks (
    publication_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 4095),
    surface TEXT NOT NULL DEFAULT 'listening_chunk' CHECK(surface='listening_chunk'),
    chunk_key TEXT NOT NULL,
    from_day TEXT NOT NULL CHECK(length(from_day)=10 AND date(from_day) IS NOT NULL AND date(from_day)=from_day),
    until_day TEXT NOT NULL CHECK(length(until_day)=10 AND date(until_day) IS NOT NULL AND date(until_day)=until_day AND from_day<=until_day),
    encoded_bytes INTEGER NOT NULL CHECK(typeof(encoded_bytes)='integer' AND encoded_bytes BETWEEN 1 AND 1048576),
    PRIMARY KEY(publication_id,position),
    UNIQUE(publication_id,chunk_key),
    FOREIGN KEY(publication_id,surface,chunk_key) REFERENCES listening_output_files(publication_id,surface,chunk_key)
) WITHOUT ROWID;
CREATE TABLE listening_output_heads (
    slot TEXT PRIMARY KEY NOT NULL CHECK(slot IN ('current','previous')),
    publication_id INTEGER NOT NULL UNIQUE REFERENCES listening_output_publications(id)
) WITHOUT ROWID;
CREATE TRIGGER listening_output_new BEFORE INSERT ON listening_output_publications
WHEN NEW.status<>'building' BEGIN SELECT RAISE(ABORT,'Listening publication starts privately'); END;
CREATE TRIGGER listening_output_identity BEFORE UPDATE OF id,schema_version,generation_key,timezone,built_at,input_revision,revision_reason,output_count,chunk_count ON listening_output_publications
BEGIN SELECT RAISE(ABORT,'Listening publication inputs are immutable'); END;
CREATE TRIGGER listening_output_transition BEFORE UPDATE OF status ON listening_output_publications
WHEN NOT ((OLD.status='building' AND NEW.status='published' AND NEW.output_count=(SELECT count(*) FROM listening_output_files WHERE publication_id=OLD.id)
    AND NEW.chunk_count=(SELECT count(*) FROM listening_output_chunks WHERE publication_id=OLD.id)
    AND (NEW.chunk_count=0 OR ((SELECT min(position) FROM listening_output_chunks WHERE publication_id=OLD.id)=0 AND (SELECT max(position) FROM listening_output_chunks WHERE publication_id=OLD.id)=NEW.chunk_count-1)))
    OR (OLD.status='published' AND NEW.status='retiring' AND NOT EXISTS(SELECT 1 FROM listening_output_heads WHERE publication_id=OLD.id)))
BEGIN SELECT RAISE(ABORT,'Listening publication is incomplete or retained'); END;
CREATE TRIGGER listening_output_retained BEFORE DELETE ON listening_output_publications
WHEN OLD.status<>'retiring' BEGIN SELECT RAISE(ABORT,'Listening publication must retire first'); END;
CREATE TRIGGER listening_output_head_insert BEFORE INSERT ON listening_output_heads
WHEN NOT EXISTS(SELECT 1 FROM listening_output_publications WHERE id=NEW.publication_id AND status='published')
BEGIN SELECT RAISE(ABORT,'Listening head requires published output'); END;
CREATE TRIGGER listening_output_head_update BEFORE UPDATE ON listening_output_heads
WHEN NOT EXISTS(SELECT 1 FROM listening_output_publications WHERE id=NEW.publication_id AND status='published')
BEGIN SELECT RAISE(ABORT,'Listening head requires published output'); END;
CREATE TRIGGER listening_output_file_insert BEFORE INSERT ON listening_output_files
WHEN NOT EXISTS(SELECT 1 FROM listening_output_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Listening output is sealed'); END;
CREATE TRIGGER listening_output_file_update BEFORE UPDATE ON listening_output_files
WHEN NEW.id IS NOT OLD.id OR NEW.publication_id IS NOT OLD.publication_id OR NEW.surface IS NOT OLD.surface OR NEW.chunk_key IS NOT OLD.chunk_key OR NEW.content_digest IS NOT OLD.content_digest OR NEW.byte_size IS NOT OLD.byte_size OR NEW.built_at IS NOT OLD.built_at
BEGIN SELECT RAISE(ABORT,'Listening output is immutable'); END;
CREATE TRIGGER listening_output_file_delete BEFORE DELETE ON listening_output_files
WHEN NOT EXISTS(SELECT 1 FROM listening_output_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Listening output is retained'); END;
CREATE TRIGGER listening_output_chunk_insert BEFORE INSERT ON listening_output_chunks
WHEN NOT EXISTS(SELECT 1 FROM listening_output_publications WHERE id=NEW.publication_id AND status='building')
BEGIN SELECT RAISE(ABORT,'Listening chunk index is sealed'); END;
CREATE TRIGGER listening_output_chunk_update BEFORE UPDATE ON listening_output_chunks
BEGIN SELECT RAISE(ABORT,'Listening chunk index is immutable'); END;
CREATE TRIGGER listening_output_chunk_delete BEFORE DELETE ON listening_output_chunks
WHEN NOT EXISTS(SELECT 1 FROM listening_output_publications WHERE id=OLD.publication_id AND status IN ('building','retiring'))
BEGIN SELECT RAISE(ABORT,'Listening chunk index is retained'); END;
CREATE TRIGGER retired_listening_output_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface IN ('listening_analytics','listening_edition') OR substr(NEW.surface,1,16)='listening_chunk:'
BEGIN SELECT RAISE(ABORT,'Listening output has a publication owner'); END;
CREATE TRIGGER retired_listening_output_update BEFORE UPDATE ON rec_proposals
WHEN NEW.surface IN ('listening_analytics','listening_edition') OR substr(NEW.surface,1,16)='listening_chunk:'
BEGIN SELECT RAISE(ABORT,'Listening output has a publication owner'); END;
