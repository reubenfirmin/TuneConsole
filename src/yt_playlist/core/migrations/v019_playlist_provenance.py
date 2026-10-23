"""Generation and adoption are durable policy, independent of user organization."""
from .runner import MigrationError


TABLE_SQL = """CREATE TABLE playlist_provenance (
    ytm TEXT PRIMARY KEY NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('tuneconsole','ordinary')),
    adopted INTEGER NOT NULL CHECK(adopted IN (0,1)),
    description TEXT
)"""
VIEW_SQL = """CREATE VIEW quarantined_playlists AS SELECT ytm,'Generated' name
FROM playlist_provenance WHERE origin='tuneconsole' AND adopted=0"""


def apply(conn, context):
    conn.execute(TABLE_SQL)
    conn.execute(VIEW_SQL)
    conn.execute("INSERT INTO playlist_provenance(ytm,origin,adopted) "
                 "SELECT ytm,'tuneconsole',0 FROM playlist_group WHERE name='Generated'")
    # Recipes prove origin. Outside the old quarantine, preserve the user's adoption.
    conn.execute("INSERT OR IGNORE INTO playlist_provenance(ytm,origin,adopted) "
                 "SELECT playlist_ytm,'tuneconsole',1 FROM rec_recipes")


def validate(conn):
    for name, sql in (('playlist_provenance', TABLE_SQL), ('quarantined_playlists', VIEW_SQL)):
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not row or row[0] != sql:
            raise MigrationError(f'Incompatible playlist provenance: {name}')
