"""Bind enabled and explicitly disabled vocabulary choices to genre identity."""
import json
from pathlib import Path
import sqlite3

from yt_playlist.core import genre_identity
from yt_playlist.util import genre_catalog
from .runner import MigrationError

OBJECTS = frozenset({'genre_preferences', 'ix_genre_preference_enabled'})


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_genre_preferences.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete genre preference schema')


def retire_preparation_capture(conn):
    """Detach only known capture programs whose installed source is retired."""
    from ..preparation.genre_preferences import CHOICES
    from .current import _tokens
    from .v103_entity_names import transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for name, sql in CHOICES.definitions(conn):
        if not name.startswith('prepare_genre_choices_dependency_'):
            continue
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is not None:
            if _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
                raise MigrationError('Incompatible retired genre capture: '+name)
            conn.execute('DROP TRIGGER '+name)


def apply(conn, context):
    genre_identity.seed_catalog(conn)
    enabled = [row[0] for row in conn.execute('SELECT name FROM genre_whitelist')]
    settings = dict(conn.execute("SELECT key,value FROM settings WHERE key IN ('genres_seeded','genre_defaults')"))
    try:
        baseline = json.loads(settings['genre_defaults']) if 'genre_defaults' in settings else genre_catalog.legacy_names()
        if not isinstance(baseline, list) or any(not isinstance(label, str) for label in baseline):
            raise ValueError('genre_defaults requires a list of labels')
        # Ambiguous enabled preferences stop cutover before changing owners.
        for label in enabled:
            if genre_identity.resolve(conn, label)[1] == 'ambiguous_genre':
                raise ValueError('Ambiguous enabled genre: ' + label)
    except (ValueError, TypeError) as exc:
        raise MigrationError(f'Invalid genre preferences; original retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    if settings.get('genres_seeded') or enabled:
        # An omitted old default is disabled; a never-introduced default is
        # absent and may be added by a future catalog revision.
        for label in baseline:
            key, reason = genre_identity.resolve(conn, label)
            if key is not None:
                conn.execute("INSERT OR IGNORE INTO genre_preferences VALUES (?,0,'default')", (key,))
    for label in enabled:
        try:
            key = genre_identity.add_custom(conn, label)
        except ValueError as exc:
            raise MigrationError(f'Invalid genre preference {label!r}; original retained: {exc}') from exc
        conn.execute("INSERT INTO genre_preferences VALUES (?,1,'legacy') ON CONFLICT(genre_key) "
                     "DO UPDATE SET enabled=1,origin='legacy'", (key,))
    # Explicit historical upgrade retires this source. Its preparation evidence
    # remains retained, but catalog capture cannot keep querying a dropped owner.
    # SQLite validates trigger programs during later ALTER TABLE operations.
    retire_preparation_capture(conn)
    conn.execute('DROP TABLE genre_whitelist')
