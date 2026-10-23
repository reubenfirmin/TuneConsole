"""Retain artist-suggestion provenance and standardize Spotify history sources."""
from .runner import MigrationError


SQL = (
    "CREATE TABLE discovery_artist_sources (artist_id INTEGER NOT NULL REFERENCES artists(id),source TEXT NOT NULL,identity_id INTEGER REFERENCES identities(id),score REAL NOT NULL,PRIMARY KEY(artist_id,source))",
    "CREATE INDEX discovery_artist_sources_identity ON discovery_artist_sources(identity_id,source)",
    "CREATE TABLE spotify_history_listens (identity_id INTEGER NOT NULL REFERENCES identities(id),track_uri TEXT NOT NULL,ended_at REAL NOT NULL,event_id INTEGER REFERENCES play_events(id) ON DELETE SET NULL,artist_id INTEGER REFERENCES artists(id),PRIMARY KEY(identity_id,track_uri,ended_at))",
)


def apply(conn, context):
    for sql in SQL:
        conn.execute(sql)
    # Existing suggestions have no trustworthy source attribution. Preserve them;
    # never guess that an independently found artist came from an old import.
    conn.execute("INSERT INTO discovery_artist_sources SELECT artist_id,'unknown',NULL,COALESCE(score,0) "
                 'FROM discovered_artists')
    if conn.execute("SELECT 1 FROM play_event_sources WHERE source IN ('spotify','spotify_export') "
                    "UNION ALL SELECT 1 FROM listening_observations WHERE source IN ('spotify','spotify_export') "
                    "UNION ALL SELECT 1 FROM eligible_listening_evidence "
                    "WHERE source IN ('spotify','spotify_export') LIMIT 1").fetchone():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('spotify_legacy_discovery_unattributed','1')")
    conn.execute("INSERT OR IGNORE INTO play_event_sources(event_id,source) "
                 "SELECT event_id,'spotify_export' FROM play_event_sources WHERE source='spotify'")
    conn.execute("DELETE FROM play_event_sources WHERE source='spotify'")
    conn.execute("UPDATE play_events SET source='spotify_export' WHERE source='spotify'")


def validate(conn):
    for sql in SQL:
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0] != sql:
            raise MigrationError(f'Incompatible Spotify history provenance: {name}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid Spotify history provenance reference')
