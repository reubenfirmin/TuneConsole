CREATE TABLE recipe_artists (
 recipe_id INTEGER NOT NULL REFERENCES road_trip_recipes(id) ON DELETE CASCADE,
 artist_id INTEGER NOT NULL REFERENCES artists(id),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 PRIMARY KEY(recipe_id,artist_id), UNIQUE(recipe_id,position)
) WITHOUT ROWID;
CREATE INDEX ix_recipe_artist ON recipe_artists(artist_id,recipe_id);
CREATE TABLE recipe_genres (
 recipe_id INTEGER NOT NULL REFERENCES road_trip_recipes(id) ON DELETE CASCADE,
 genre_key TEXT NOT NULL REFERENCES genre_catalog(key),
 role TEXT NOT NULL CHECK(role IN ('include','exclude')),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 PRIMARY KEY(recipe_id,genre_key,role), UNIQUE(recipe_id,role,position)
) WITHOUT ROWID;
CREATE INDEX ix_recipe_genre ON recipe_genres(genre_key,recipe_id);
CREATE TABLE recipe_unresolved_artists (
 recipe_id INTEGER NOT NULL REFERENCES road_trip_recipes(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 raw_label TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason='unknown_or_ambiguous_artist'),
 PRIMARY KEY(recipe_id,position)
) WITHOUT ROWID;
CREATE TABLE recipe_unresolved_genres (
 recipe_id INTEGER NOT NULL REFERENCES road_trip_recipes(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('include','exclude')),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 raw_label TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('unknown_genre','ambiguous_genre')),
 PRIMARY KEY(recipe_id,role,position)
) WITHOUT ROWID;
CREATE TRIGGER recipe_artist_position BEFORE INSERT ON recipe_artists
WHEN EXISTS(SELECT 1 FROM recipe_unresolved_artists WHERE recipe_id=NEW.recipe_id AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains an unresolved artist'); END;
CREATE TRIGGER recipe_unresolved_artist_position BEFORE INSERT ON recipe_unresolved_artists
WHEN EXISTS(SELECT 1 FROM recipe_artists WHERE recipe_id=NEW.recipe_id AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains an artist'); END;
CREATE TRIGGER recipe_genre_position BEFORE INSERT ON recipe_genres
WHEN EXISTS(SELECT 1 FROM recipe_unresolved_genres WHERE recipe_id=NEW.recipe_id AND role=NEW.role AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains an unresolved genre'); END;
CREATE TRIGGER recipe_unresolved_genre_position BEFORE INSERT ON recipe_unresolved_genres
WHEN EXISTS(SELECT 1 FROM recipe_genres WHERE recipe_id=NEW.recipe_id AND role=NEW.role AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains a genre'); END;
CREATE TRIGGER recipe_artist_position_update BEFORE UPDATE ON recipe_artists
WHEN EXISTS(SELECT 1 FROM recipe_unresolved_artists WHERE recipe_id=NEW.recipe_id AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains an unresolved artist'); END;
CREATE TRIGGER recipe_unresolved_artist_position_update BEFORE UPDATE ON recipe_unresolved_artists
WHEN EXISTS(SELECT 1 FROM recipe_artists WHERE recipe_id=NEW.recipe_id AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains an artist'); END;
CREATE TRIGGER recipe_genre_position_update BEFORE UPDATE ON recipe_genres
WHEN EXISTS(SELECT 1 FROM recipe_unresolved_genres WHERE recipe_id=NEW.recipe_id AND role=NEW.role AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains an unresolved genre'); END;
CREATE TRIGGER recipe_unresolved_genre_position_update BEFORE UPDATE ON recipe_unresolved_genres
WHEN EXISTS(SELECT 1 FROM recipe_genres WHERE recipe_id=NEW.recipe_id AND role=NEW.role AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'Recipe position already contains a genre'); END;
