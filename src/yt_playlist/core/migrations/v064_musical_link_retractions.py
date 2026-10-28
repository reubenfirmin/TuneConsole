"""Retire source contributions through the durable recording preparation queue."""

SQL = ("CREATE TRIGGER musical_link_delete AFTER DELETE ON rec_musical_links BEGIN "
       "INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings WHERE id=OLD.recording_id "
       "ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1; END")


def apply(conn, context):
    conn.execute(SQL)
    # Earlier contractions could leave a last-good profile stranded with no
    # pending work. Queue profiles retaining a source digest no longer linked
    # to their recording; the ordinary bounded worker rebuilds them afterward.
    conn.execute("""INSERT INTO rec_content_pending(identity_key)
        SELECT p.identity_key FROM rec_musical_profiles p JOIN recordings r USING(identity_key)
        WHERE EXISTS (SELECT 1 FROM json_each(p.payload,'$.source_versions') old
            WHERE NOT EXISTS (SELECT 1 FROM rec_musical_links l
                JOIN rec_musical_source_profiles s USING(source_key)
                WHERE l.recording_id=r.id AND s.digest=old.value))
        ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1""")


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='musical_link_delete'").fetchone()
    if row is None or _tokens(row[0]) != _tokens(SQL):
        raise MigrationError('Incompatible musical link retraction trigger')
