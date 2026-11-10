CREATE TABLE genre_preferences (
    genre_key TEXT PRIMARY KEY NOT NULL REFERENCES genre_catalog(key),
    enabled INTEGER NOT NULL CHECK(typeof(enabled)='integer' AND enabled IN (0,1)),
    origin TEXT NOT NULL CHECK(origin IN ('default','custom','user','legacy'))
) WITHOUT ROWID;
CREATE INDEX ix_genre_preference_enabled ON genre_preferences(enabled,genre_key);
