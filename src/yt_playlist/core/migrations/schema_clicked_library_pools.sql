CREATE TABLE clicked_library_pools (
    generation_id INTEGER PRIMARY KEY NOT NULL REFERENCES recommendation_generations(id) ON DELETE CASCADE,
    lane TEXT NOT NULL CHECK(lane IN ('wheelhouse','explore','comfort','temporal')),
    epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=0),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308))
);
CREATE TABLE clicked_library_genres (
    generation_id INTEGER NOT NULL REFERENCES clicked_library_pools(generation_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('include','avoid')),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 99),
    raw_key TEXT NOT NULL CHECK(length(raw_key)>0),
    family_key TEXT REFERENCES genre_family(key),
    genre_key TEXT REFERENCES genre_catalog(key),
    resolution_reason TEXT,
    CHECK((family_key IS NOT NULL AND genre_key IS NULL AND resolution_reason IS NULL) OR
          (family_key IS NULL AND genre_key IS NOT NULL AND resolution_reason IS NULL) OR
          (family_key IS NULL AND genre_key IS NULL AND resolution_reason IS NOT NULL AND resolution_reason IN ('unknown_genre','ambiguous_genre'))),
    PRIMARY KEY(generation_id,kind,position),
    UNIQUE(generation_id,kind,raw_key)
) WITHOUT ROWID;
CREATE TRIGGER clicked_library_pool_owner BEFORE INSERT ON clicked_library_pools WHEN NOT EXISTS (
    SELECT 1 FROM recommendation_generation_surfaces WHERE generation_id=NEW.generation_id AND surface='home_pool:'||NEW.lane)
BEGIN SELECT RAISE(ABORT,'Clicked pool must belong to its lane publication'); END;
CREATE TRIGGER clicked_library_pool_update BEFORE UPDATE ON clicked_library_pools
BEGIN SELECT RAISE(ABORT,'Clicked pool parameters are immutable'); END;
CREATE TRIGGER clicked_library_pool_delete BEFORE DELETE ON clicked_library_pools WHEN EXISTS (
    SELECT 1 FROM recommendation_heads WHERE generation_id=OLD.generation_id)
BEGIN SELECT RAISE(ABORT,'Clicked pool parameters are retained'); END;
CREATE TRIGGER clicked_library_pool_insert BEFORE INSERT ON clicked_library_pools WHEN EXISTS (
    SELECT 1 FROM recommendation_heads WHERE generation_id=NEW.generation_id)
BEGIN SELECT RAISE(ABORT,'Published clicked pool parameters are immutable'); END;
CREATE TRIGGER clicked_library_genre_update BEFORE UPDATE ON clicked_library_genres
BEGIN SELECT RAISE(ABORT,'Clicked pool genres are immutable'); END;
CREATE TRIGGER clicked_library_genre_insert BEFORE INSERT ON clicked_library_genres WHEN EXISTS (
    SELECT 1 FROM recommendation_heads WHERE generation_id=NEW.generation_id)
BEGIN SELECT RAISE(ABORT,'Published clicked pool genres are immutable'); END;
CREATE TRIGGER clicked_library_genre_delete BEFORE DELETE ON clicked_library_genres WHEN EXISTS (
    SELECT 1 FROM recommendation_heads WHERE generation_id=OLD.generation_id)
BEGIN SELECT RAISE(ABORT,'Clicked pool genres are retained'); END;
