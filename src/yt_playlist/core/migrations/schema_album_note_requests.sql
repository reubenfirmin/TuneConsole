CREATE TABLE album_note_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id>=1),
    browse_id TEXT NOT NULL UNIQUE CHECK(typeof(browse_id)='text' AND length(browse_id)>0),
    provider TEXT NOT NULL DEFAULT 'youtube' CHECK(provider='youtube'),
    album_provider_id TEXT,
    unresolved_reason TEXT,
    artist TEXT NOT NULL CHECK(typeof(artist)='text' AND length(artist)>0),
    title TEXT NOT NULL CHECK(typeof(title)='text' AND length(title)>0),
    year_text TEXT,
    year_number INTEGER CHECK(year_number IS NULL OR typeof(year_number)='integer'),
    thumbnail TEXT,
    requested_until REAL CHECK(requested_until IS NULL OR (typeof(requested_until) IN ('real','integer') AND abs(requested_until)<=1.7976931348623157e308)),
    revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
    requested_at REAL CHECK(requested_at IS NULL OR (typeof(requested_at) IN ('real','integer') AND abs(requested_at)<=1.7976931348623157e308)),
    updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('real','integer') AND abs(updated_at)<=1.7976931348623157e308)),
    FOREIGN KEY(provider,album_provider_id) REFERENCES album_provider_ids(provider,provider_id),
    CHECK((album_provider_id IS NOT NULL AND album_provider_id=browse_id AND unresolved_reason IS NULL) OR
          (album_provider_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='provider_album_not_admitted')),
    CHECK(year_text IS NULL OR year_number IS NULL)
);
CREATE TRIGGER album_note_request_budget BEFORE INSERT ON album_note_requests
WHEN (SELECT count(*) FROM album_note_requests)>=24
BEGIN SELECT RAISE(ABORT,'Album note demand exceeds its queue budget'); END;
CREATE TRIGGER album_note_request_identity BEFORE UPDATE OF id,browse_id,provider,requested_at ON album_note_requests
WHEN NEW.id<>OLD.id OR NEW.browse_id<>OLD.browse_id OR NEW.provider<>OLD.provider OR NEW.requested_at IS NOT OLD.requested_at
BEGIN SELECT RAISE(ABORT,'Album note request identity is immutable'); END;
CREATE TRIGGER album_note_request_revision BEFORE UPDATE ON album_note_requests
WHEN NEW.revision<=OLD.revision
BEGIN SELECT RAISE(ABORT,'Album note request revision must advance'); END;
CREATE TRIGGER retired_album_note_request_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface='album_note_requests'
BEGIN SELECT RAISE(ABORT,'Album note demand has a typed owner'); END;
CREATE TRIGGER retired_album_note_request_update BEFORE UPDATE OF surface,payload ON rec_proposals
WHEN NEW.surface='album_note_requests'
BEGIN SELECT RAISE(ABORT,'Album note demand has a typed owner'); END;
CREATE TABLE album_note_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id>=1),
    browse_id TEXT NOT NULL UNIQUE CHECK(typeof(browse_id)='text' AND length(browse_id)>0),
    provider TEXT NOT NULL DEFAULT 'youtube' CHECK(provider='youtube'),
    album_provider_id TEXT,
    unresolved_reason TEXT,
    filename TEXT NOT NULL CHECK(typeof(filename)='text' AND length(filename)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('real','integer') AND abs(built_at)<=1.7976931348623157e308)),
    FOREIGN KEY(provider,album_provider_id) REFERENCES album_provider_ids(provider,provider_id),
    CHECK((album_provider_id IS NOT NULL AND album_provider_id=browse_id AND unresolved_reason IS NULL) OR
          (album_provider_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='provider_album_not_admitted'))
);
CREATE TRIGGER album_note_output_budget BEFORE INSERT ON album_note_outputs
WHEN (SELECT count(*) FROM album_note_outputs)>=128
BEGIN SELECT RAISE(ABORT,'Album note output exceeds its cache budget'); END;
CREATE TRIGGER album_note_output_immutable BEFORE UPDATE ON album_note_outputs
WHEN NEW.id<>OLD.id OR NEW.browse_id<>OLD.browse_id OR NEW.provider<>OLD.provider
  OR NEW.album_provider_id IS NOT OLD.album_provider_id OR NEW.unresolved_reason IS NOT OLD.unresolved_reason
  OR NEW.content_digest<>OLD.content_digest OR NEW.byte_size<>OLD.byte_size OR NEW.built_at IS NOT OLD.built_at
BEGIN SELECT RAISE(ABORT,'Replace album note output instead of mutating it'); END;
CREATE TRIGGER retired_album_note_output_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface='album_notes'
BEGIN SELECT RAISE(ABORT,'Album notes have a file owner'); END;
CREATE TRIGGER retired_album_note_output_update BEFORE UPDATE OF surface,payload ON rec_proposals
WHEN NEW.surface='album_notes'
BEGIN SELECT RAISE(ABORT,'Album notes have a file owner'); END;
