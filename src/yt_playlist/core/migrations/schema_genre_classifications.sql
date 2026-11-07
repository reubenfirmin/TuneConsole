CREATE TABLE genre_redirects (
 retired_key TEXT PRIMARY KEY NOT NULL REFERENCES genre_catalog(key),
 genre_key TEXT NOT NULL REFERENCES genre_catalog(key),
 CHECK(retired_key<>genre_key)
) WITHOUT ROWID;
CREATE TRIGGER genre_redirect_current_insert BEFORE INSERT ON genre_redirects
WHEN EXISTS(SELECT 1 FROM genre_redirects WHERE retired_key=NEW.genre_key OR genre_key=NEW.retired_key)
BEGIN SELECT RAISE(ABORT,'Genre redirects must point directly to a current identity'); END;
CREATE TRIGGER genre_redirect_current_update BEFORE UPDATE ON genre_redirects
WHEN NEW.retired_key<>OLD.retired_key OR EXISTS(SELECT 1 FROM genre_redirects WHERE retired_key=NEW.genre_key)
BEGIN SELECT RAISE(ABORT,'Genre redirects must point directly to a current identity'); END;
CREATE TRIGGER genre_redirect_retained BEFORE DELETE ON genre_redirects
BEGIN SELECT RAISE(ABORT,'Genre merge lineage is retained'); END;
CREATE INDEX ix_genre_catalog_name ON genre_catalog(name COLLATE NOCASE);
CREATE INDEX ix_genre_alias_label ON genre_alias(label COLLATE NOCASE);
CREATE INDEX ix_genre_alias_owner ON genre_alias(genre_key);
CREATE TABLE genre_merge_preferences (
 id INTEGER PRIMARY KEY,
 retired_key TEXT NOT NULL REFERENCES genre_catalog(key),
 genre_key TEXT NOT NULL REFERENCES genre_catalog(key),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 origin TEXT NOT NULL,
 merged_at REAL NOT NULL
);
CREATE INDEX ix_recordings_genre ON recordings(genre_key);
CREATE INDEX ix_albums_genre ON albums(genre_key);
CREATE INDEX ix_discovered_artists_genre ON discovered_artists(genre_key);
CREATE TABLE genre_classification_legacy (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER REFERENCES recordings(id),
    album_id INTEGER REFERENCES albums(id),
    artist_id INTEGER REFERENCES discovered_artists(artist_id),
    raw_label TEXT NOT NULL,
    genre_key TEXT REFERENCES genre_catalog(key),
    resolution_reason TEXT CHECK(resolution_reason IN ('unknown_genre','ambiguous_genre')),
    CHECK((recording_id IS NOT NULL)+(album_id IS NOT NULL)+(artist_id IS NOT NULL)=1),
    CHECK((genre_key IS NOT NULL AND resolution_reason IS NULL)
        OR (genre_key IS NULL AND resolution_reason IS NOT NULL) OR raw_label='')
);
CREATE UNIQUE INDEX ix_legacy_genre_recording_id ON genre_classification_legacy(recording_id) WHERE recording_id IS NOT NULL;
CREATE UNIQUE INDEX ix_legacy_genre_album_id ON genre_classification_legacy(album_id) WHERE album_id IS NOT NULL;
CREATE UNIQUE INDEX ix_legacy_genre_artist_id ON genre_classification_legacy(artist_id) WHERE artist_id IS NOT NULL;
CREATE TRIGGER immutable_genre_classification_legacy BEFORE UPDATE ON genre_classification_legacy BEGIN SELECT RAISE(ABORT,'Captured genre labels are immutable'); END;
