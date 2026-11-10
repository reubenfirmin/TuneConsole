ALTER TABLE album_note_requests ADD COLUMN work_revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(work_revision)='integer' AND work_revision>=1);
CREATE TRIGGER album_note_request_work_revision BEFORE UPDATE ON album_note_requests
WHEN NEW.work_revision<OLD.work_revision OR
  ((NEW.artist IS NOT OLD.artist OR NEW.title IS NOT OLD.title OR NEW.year_text IS NOT OLD.year_text
    OR NEW.year_number IS NOT OLD.year_number OR NEW.thumbnail IS NOT OLD.thumbnail
    OR NEW.album_provider_id IS NOT OLD.album_provider_id)
   AND NEW.work_revision<=OLD.work_revision)
BEGIN SELECT RAISE(ABORT,'Changed album note inputs require a newer work revision'); END;
CREATE TABLE listening_choice_windows (
    format TEXT PRIMARY KEY NOT NULL CHECK(format IN ('albums','mixes','radio')),
    opportunity_key TEXT NOT NULL CHECK(typeof(opportunity_key)='text' AND length(CAST(opportunity_key AS BLOB)) BETWEEN 1 AND 64),
    window_text TEXT NOT NULL CHECK(typeof(window_text)='text' AND length(CAST(window_text AS BLOB))<=1024),
    updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308))
) WITHOUT ROWID;
CREATE TABLE album_menu_refresh_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    epoch INTEGER CHECK(epoch IS NULL OR (typeof(epoch)='integer' AND epoch>=0)),
    updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308))
);
CREATE TABLE album_menu_refresh_exclusions (
    request_id INTEGER NOT NULL DEFAULT 1 REFERENCES album_menu_refresh_state(id),
    position INTEGER PRIMARY KEY CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 99),
    browse_id TEXT NOT NULL CHECK(typeof(browse_id)='text' AND length(browse_id)>0),
    provider TEXT NOT NULL CHECK(provider='youtube'),
    album_provider_id TEXT,
    unresolved_reason TEXT,
    FOREIGN KEY(provider,album_provider_id) REFERENCES album_provider_ids(provider,provider_id),
    CHECK((album_provider_id IS NOT NULL AND album_provider_id=browse_id AND unresolved_reason IS NULL) OR
          (album_provider_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='provider_album_not_admitted'))
);
CREATE TABLE home_genre_requests (
    id INTEGER PRIMARY KEY CHECK(id=1),
    epoch INTEGER CHECK(epoch IS NULL OR (typeof(epoch)='integer' AND epoch>=0)),
    has_includes INTEGER NOT NULL CHECK(typeof(has_includes)='integer' AND has_includes IN (0,1)),
    has_avoids INTEGER NOT NULL CHECK(typeof(has_avoids)='integer' AND has_avoids IN (0,1)),
    held_by_vibe INTEGER CHECK(held_by_vibe IS NULL OR (typeof(held_by_vibe)='integer' AND held_by_vibe IN (0,1))),
    suspended_by_vibe INTEGER CHECK(suspended_by_vibe IS NULL OR (typeof(suspended_by_vibe)='integer' AND suspended_by_vibe IN (0,1))),
    updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)),
    CHECK(epoch IS NOT NULL OR (has_includes=0 AND has_avoids=0 AND held_by_vibe IS NULL AND suspended_by_vibe IS NULL))
);
CREATE TABLE home_genre_request_items (
    request_id INTEGER NOT NULL DEFAULT 1 REFERENCES home_genre_requests(id),
    kind TEXT NOT NULL CHECK(kind IN ('include','avoid')),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 99),
    raw_key TEXT NOT NULL CHECK(typeof(raw_key)='text' AND length(raw_key)>0),
    family_key TEXT REFERENCES genre_family(key),
    genre_key TEXT REFERENCES genre_catalog(key),
    resolution_reason TEXT CHECK(resolution_reason IN ('ambiguous_genre','unknown_genre')),
    PRIMARY KEY(kind,position),
    CHECK((family_key IS NOT NULL AND genre_key IS NULL AND resolution_reason IS NULL) OR
          (family_key IS NULL AND genre_key IS NOT NULL AND resolution_reason IS NULL) OR
          (family_key IS NULL AND genre_key IS NULL AND resolution_reason IS NOT NULL))
) WITHOUT ROWID;
CREATE TRIGGER album_menu_exclusion_owner BEFORE INSERT ON album_menu_refresh_exclusions
WHEN NOT EXISTS(SELECT 1 FROM album_menu_refresh_state WHERE id=NEW.request_id AND epoch IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'Album exclusion requires an active refresh request'); END;
CREATE TRIGGER home_genre_request_item_owner BEFORE INSERT ON home_genre_request_items
WHEN NOT EXISTS(SELECT 1 FROM home_genre_requests WHERE id=NEW.request_id AND
               ((NEW.kind='include' AND has_includes=1) OR (NEW.kind='avoid' AND has_avoids=1)))
BEGIN SELECT RAISE(ABORT,'Genre demand item requires its enabled request list'); END;
CREATE TRIGGER home_genre_request_item_update_owner BEFORE UPDATE OF request_id,kind ON home_genre_request_items
WHEN NOT EXISTS(SELECT 1 FROM home_genre_requests WHERE id=NEW.request_id AND
               ((NEW.kind='include' AND has_includes=1) OR (NEW.kind='avoid' AND has_avoids=1)))
BEGIN SELECT RAISE(ABORT,'Genre demand item requires its enabled request list'); END;
CREATE TRIGGER home_genre_request_list_owner BEFORE UPDATE OF has_includes,has_avoids ON home_genre_requests
WHEN EXISTS(SELECT 1 FROM home_genre_request_items WHERE request_id=OLD.id AND
            ((kind='include' AND NEW.has_includes=0) OR (kind='avoid' AND NEW.has_avoids=0)))
BEGIN SELECT RAISE(ABORT,'Retire genre demand items before disabling their list'); END;
CREATE TRIGGER retired_recommendation_request_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface IN ('album_menu_refresh','home_genre_request','choice_window:albums','choice_window:mixes','choice_window:radio')
BEGIN SELECT RAISE(ABORT,'Recommendation demand has typed owners'); END;
CREATE TRIGGER retired_recommendation_request_update BEFORE UPDATE OF surface,payload ON rec_proposals
WHEN NEW.surface IN ('album_menu_refresh','home_genre_request','choice_window:albums','choice_window:mixes','choice_window:radio')
BEGIN SELECT RAISE(ABORT,'Recommendation demand has typed owners'); END;
