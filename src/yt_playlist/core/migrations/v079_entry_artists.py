"""Name the ordered selected credit projection for its exact catalog entry."""
from .runner import MigrationError


TABLE = ('CREATE TABLE entry_artists (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),'
         'position INTEGER NOT NULL CHECK(position>=0),artist_id INTEGER NOT NULL REFERENCES artists(id),'
         'credit TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(entry_id,position))')


def statements():
    yield 'CREATE INDEX ix_entry_artist_subject ON entry_artists(artist_id,entry_id)'
    for purpose in ('capture', 'evidence'):
        for event in ('insert', 'delete', 'update'):
            condition = ' WHEN OLD.artist_id IS NOT NEW.artist_id OR OLD.credit IS NOT NEW.credit' if event == 'update' else ''
            mutation = ("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                        'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1;' if purpose == 'capture' else
                        'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1;')
            yield (f'CREATE TRIGGER rec_{purpose}_entry_artists_{event} AFTER {event.upper()} ON entry_artists'
                   f'{condition} BEGIN {mutation} END')


def apply(conn, context):
    # SQLite rewrites dependent trigger/view/FK references transactionally. No
    # projection row changes and no replay of learning/invalidation triggers.
    conn.execute('ALTER TABLE track_artists RENAME TO entry_artists')
    conn.execute('DROP INDEX ix_track_artist_subject')
    for purpose in ('capture', 'evidence'):
        for event in ('insert', 'delete', 'update'):
            conn.execute(f'DROP TRIGGER rec_{purpose}_track_artists_{event}')
    for sql in statements():
        conn.execute(sql)


def validate(conn):
    from . import current
    from . import v058_predicted_vibes
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN ('track_artists','ix_track_artist_subject')").fetchone():
        raise MigrationError('Retired artist projection name remains')
    expected = current._schema_contract((TABLE,))['entry_artists']
    actual = conn.execute("SELECT sql FROM sqlite_schema WHERE name='entry_artists'").fetchone()
    if actual is None or current._table(conn, 'entry_artists', actual[0]) != expected[1]:
        raise MigrationError('Incompatible ordered entry artist projection')
    for sql in statements():
        name = sql.split()[2]
        actual = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or current._tokens(actual[0]) != current._tokens(sql):
            raise MigrationError('Incompatible entry artist projection dependency: ' + name)
    # ALTER TABLE also rewrites these retained triggers' credit references.
    for sql in v058_predicted_vibes.statements():
        if not sql.startswith('CREATE TRIGGER vibe_artists_'):
            continue
        name = sql.split()[2]
        actual = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or current._tokens(actual[0]) != current._tokens(sql.replace('track_artists', 'entry_artists')):
            raise MigrationError('Incompatible entry artist prediction dependency: ' + name)
