CREATE TABLE taste_axes (
 id INTEGER PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('genre','family','other','unresolved_genre')),
 genre_key TEXT REFERENCES genre_catalog(key),
 family_key TEXT REFERENCES genre_family(key),
 value TEXT,
 resolution_reason TEXT CHECK(resolution_reason IN ('unknown_genre','ambiguous_genre')),
 CHECK((kind='genre' AND genre_key IS NOT NULL AND family_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
    OR (kind='family' AND family_key IS NOT NULL AND genre_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
    OR (kind='other' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND length(value)>0 AND value NOT LIKE 'genre:%' AND value NOT LIKE 'family:%' AND resolution_reason IS NULL)
    OR (kind='unresolved_genre' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND resolution_reason IS NOT NULL))
);
CREATE UNIQUE INDEX ix_taste_axis_genre ON taste_axes(genre_key) WHERE kind='genre';
CREATE UNIQUE INDEX ix_taste_axis_family ON taste_axes(family_key) WHERE kind='family';
CREATE UNIQUE INDEX ix_taste_axis_value ON taste_axes(kind,value) WHERE value IS NOT NULL;
CREATE TABLE taste_axis_legacy (
 table_name TEXT NOT NULL,
 row_key TEXT NOT NULL,
 raw_axis TEXT NOT NULL,
 axis_id INTEGER NOT NULL REFERENCES taste_axes(id),
 weight REAL,
 lean_value REAL,
 score REAL,
 updated_at REAL,
 last_graduated_day TEXT,
 source TEXT,
 factor REAL,
 new_weight REAL,
 created_at REAL,
 PRIMARY KEY(table_name,row_key)
) WITHOUT ROWID;
