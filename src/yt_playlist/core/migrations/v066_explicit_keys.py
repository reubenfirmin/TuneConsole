"""Explicit logical keys, with stable mood IDs and canonical seed membership.

The runner disables FK actions only for this atomic table replacement, checks
all references before commit, and restores the caller's enforcement setting.
Invalid keys are diagnosed before any DDL; their original rows remain intact.
"""
import json

from .runner import MigrationError


# Frozen upgrade definitions. stale_dismissed exists only in installed history;
# retaining its four user choices does not introduce it into fresh databases.
TABLES = {
    'rec_group_publications': (
        'generation', ('generation',),
        'CREATE TABLE rec_group_publications (generation TEXT PRIMARY KEY NOT NULL '
        'REFERENCES rec_group_generations(generation)) WITHOUT ROWID'),
    'rec_group_builds': (
        'generation', ('generation', 'owner', 'expires_at', 'ready'),
        'CREATE TABLE rec_group_builds (generation TEXT PRIMARY KEY NOT NULL '
        'REFERENCES rec_group_generations(generation),owner TEXT NOT NULL,'
        'expires_at REAL NOT NULL,ready INTEGER NOT NULL DEFAULT 0 CHECK(ready IN (0,1))) WITHOUT ROWID'),
    'stale_dismissed': (
        'ytm',
        ('ytm', 'until',),
        """CREATE TABLE stale_dismissed (
  ytm TEXT PRIMARY KEY NOT NULL, until REAL   -- until NULL = dismissed forever; else snoozed until ts
)"""),
    'settings': (
        'key',
        ('key', 'value',),
        """CREATE TABLE settings (
  key TEXT PRIMARY KEY NOT NULL, value TEXT   -- small app settings, e.g. lastfm_api_key
)"""),
    'genre_whitelist': (
        'name',
        ('name',),
        """CREATE TABLE genre_whitelist (
  name TEXT PRIMARY KEY NOT NULL COLLATE NOCASE   -- editable genre whitelist for tag matching
)"""),
    'rec_vectors': (
        'identity_key',
        ('identity_key', 'vec',),
        """CREATE TABLE rec_vectors (
  identity_key TEXT PRIMARY KEY NOT NULL,
  vec BLOB NOT NULL                       -- float32 taste-embedding for the track (see embed.py)
)"""),
    'rec_weights': (
        'axis',
        ('axis', 'weight', 'updated_at',),
        """CREATE TABLE rec_weights (
  axis TEXT PRIMARY KEY NOT NULL,                  -- e.g. 'lane:deep_cut', 'family:techno'
  weight REAL NOT NULL DEFAULT 1.0        -- learned blend weight; 1.0 = prior
, updated_at REAL)"""),
    'rec_proposals': (
        'surface',
        ('surface', 'payload', 'built_at',),
        """CREATE TABLE rec_proposals (
  surface TEXT PRIMARY KEY NOT NULL,               -- 'auto_playlists' | 'discover'
  payload TEXT NOT NULL,                  -- JSON, materialized by the rec worker (last-good serving)
  built_at REAL
)"""),
    'rec_artist_similar': (
        'artist',
        ('artist', 'payload', 'fetched_at', 'artist_id',),
        """CREATE TABLE rec_artist_similar (
  artist TEXT PRIMARY KEY NOT NULL,                -- anchor artist (display name)
  payload TEXT NOT NULL,                  -- JSON [[name, match], ...] from Last.fm
  fetched_at REAL
, artist_id INTEGER REFERENCES artists(id))"""),
    'artist_scans': (
        'artist',
        ('artist', 'scanned_at', 'artist_id',),
        """CREATE TABLE artist_scans (
  artist TEXT PRIMARY KEY NOT NULL,
  scanned_at REAL                         -- last time we hit YT/Last.fm for this artist
, artist_id INTEGER REFERENCES artists(id))"""),
    'discovered_artists': (
        'artist',
        ('artist', 'score', 'because', 'fits', 'thumbnail', 'found_at', 'last_shown', 'genre',
         'first_shown', 'offered_count', 'legacy_offered_count', 'legacy_first_shown',
         'legacy_last_shown', 'artist_id'),
        """CREATE TABLE discovered_artists (
  artist TEXT PRIMARY KEY NOT NULL,
  score REAL, because TEXT, fits TEXT, thumbnail TEXT,
  found_at REAL, last_shown REAL
, genre TEXT, first_shown REAL, offered_count INTEGER NOT NULL DEFAULT 0, legacy_offered_count INTEGER NOT NULL DEFAULT 0, legacy_first_shown REAL, legacy_last_shown REAL, artist_id INTEGER REFERENCES artists(id))"""),
    'rec_theme': (
        'facet',
        ('facet', 'score', 'updated_at',),
        """CREATE TABLE rec_theme (
  facet      TEXT PRIMARY KEY NOT NULL,             -- 'genre:<fam>' | 'era:<decade>' | 'artist:<name>'
  score      REAL NOT NULL,                -- persistent signed running total (interaction-driven, no time decay)
  updated_at REAL NOT NULL                 -- bookkeeping only
)"""),
    'rec_lean': (
        'axis',
        ('axis', 'value', 'updated_at', 'last_graduated_day',),
        """CREATE TABLE rec_lean (
  axis TEXT PRIMARY KEY NOT NULL,                  -- 'genre:<fam>' | 'genre:<sub>' | 'era:<decade>' | 'artist:<name>'
  value REAL NOT NULL DEFAULT 1.0,        -- standing transient multiplier; 1.0 = neutral (non-decaying)
  updated_at REAL,                        -- when the slider was last moved (drives held-day exposure)
  last_graduated_day TEXT                 -- UTC date (YYYY-MM-DD) of last exposure-graduation, or NULL
)"""),
    'rec_content_vectors': (
        'identity_key',
        ('identity_key', 'vec',),
        """CREATE TABLE rec_content_vectors (
  identity_key TEXT PRIMARY KEY NOT NULL,
  vec BLOB NOT NULL                       -- float32 content (genre/era) vector; see embed.build_content_vectors
)"""),
    'rec_discovered_content_vectors': (
        'identity_key',
        ('identity_key', 'vec',),
        """CREATE TABLE rec_discovered_content_vectors (
  identity_key TEXT PRIMARY KEY NOT NULL,
  vec BLOB NOT NULL                       -- #13 P2: out-of-corpus track content vectors (same model space)
)"""),
    'home_hidden_facet': (
        'axis',
        ('axis',),
        """CREATE TABLE home_hidden_facet (
  axis TEXT PRIMARY KEY NOT NULL                   -- 'genre:<name>' | 'era:<decade>' hidden from the Home panel
)"""),
    'rec_play_grad': (
        'axis',
        ('axis', 'last_graduated_day',),
        """CREATE TABLE rec_play_grad (
  axis               TEXT PRIMARY KEY NOT NULL,      -- facet token, e.g. 'genre:techno'
  last_graduated_day TEXT                   -- UTC YYYY-MM-DD the play-exposure funnel last graduated this axis
)"""),
    'rec_artist_vectors': (
        'artist',
        ('artist', 'vec',),
        """CREATE TABLE rec_artist_vectors (
  artist TEXT PRIMARY KEY NOT NULL,                -- normalized artist name (util.matching.normalize)
  vec BLOB NOT NULL                       -- #28 float32 collaborative artist embedding (see rec/artist_model.py)
)"""),
    'rec_artist_content_vectors': (
        'artist',
        ('artist', 'vec',),
        """CREATE TABLE rec_artist_content_vectors (
  artist TEXT PRIMARY KEY NOT NULL,                -- normalized artist name
  vec BLOB NOT NULL                       -- #28 float32 artist content (genre/era/audio) vector, same model space
)"""),
    'wiki_cards': (
        'subject',
        ('subject', 'kind', 'display', 'title', 'extract', 'thumbnail', 'url', 'found', 'fetched_at',),
        """CREATE TABLE wiki_cards (
  subject    TEXT PRIMARY KEY NOT NULL,
  kind       TEXT NOT NULL,
  display    TEXT NOT NULL,
  title      TEXT,
  extract    TEXT,
  thumbnail  TEXT,
  url        TEXT,
  found      INTEGER NOT NULL,
  fetched_at REAL NOT NULL
)"""),
    'genre_family': (
        'key',
        ('key', 'name',),
        """CREATE TABLE genre_family (
  key TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL
)"""),
    'genre_catalog': (
        'key',
        ('key', 'name', 'family_key', 'source', 'family_source', 'description',),
        """CREATE TABLE genre_catalog (
  key TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
  family_key TEXT REFERENCES genre_family(key),
  source TEXT NOT NULL, family_source TEXT, description TEXT NOT NULL DEFAULT ''
)"""),
    'genre_alias': (
        'label_key',
        ('label_key', 'label', 'genre_key', 'source',),
        """CREATE TABLE genre_alias (
  label_key TEXT PRIMARY KEY NOT NULL, label TEXT NOT NULL,
  genre_key TEXT NOT NULL REFERENCES genre_catalog(key), source TEXT NOT NULL
)"""),
    'legacy_enrichment_attempts': (
        'identity_key',
        ('identity_key', 'payload', 'reason',),
        """CREATE TABLE legacy_enrichment_attempts (identity_key TEXT PRIMARY KEY NOT NULL,payload TEXT NOT NULL,reason TEXT NOT NULL)"""),
    'rec_serving_policies': (
        'model_version',
        ('model_version', 'configuration', 'created_at',),
        """CREATE TABLE rec_serving_policies (
        model_version TEXT PRIMARY KEY NOT NULL, configuration TEXT NOT NULL, created_at REAL NOT NULL
    )"""),
    'enrichment_provider_cooldowns': (
        'provider',
        ('provider', 'failures', 'retry_at', 'error',),
        """CREATE TABLE enrichment_provider_cooldowns (
    provider TEXT PRIMARY KEY NOT NULL,
    failures INTEGER NOT NULL,
    retry_at REAL NOT NULL,
    error TEXT NOT NULL
)"""),
    'enrichment_provider_config': (
        'provider',
        ('provider', 'fingerprint', 'status',),
        """CREATE TABLE enrichment_provider_config (
    provider TEXT PRIMARY KEY NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL
)"""),
    'album_share_snapshots': (
        'browse_id',
        ('browse_id', 'title', 'video_ids_json',),
        """CREATE TABLE album_share_snapshots (
    browse_id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    video_ids_json TEXT NOT NULL
)"""),
    'rec_group_generations': (
        'generation',
        ('generation', 'policy', 'fingerprint', 'created_at', 'serial', 'payload',),
        """CREATE TABLE rec_group_generations (generation TEXT PRIMARY KEY NOT NULL,policy TEXT NOT NULL,fingerprint TEXT NOT NULL,created_at REAL NOT NULL,serial INTEGER NOT NULL,payload TEXT NOT NULL)"""),
    'rec_group_identities': (
        'group_id',
        ('group_id', 'legacy_mode_id', 'created_at',),
        """CREATE TABLE rec_group_identities (group_id TEXT PRIMARY KEY NOT NULL,legacy_mode_id INTEGER UNIQUE,created_at REAL NOT NULL)"""),
    'rec_group_definitions': (
        'version',
        ('version', 'generation', 'group_id', 'parent_version', 'payload',),
        """CREATE TABLE rec_group_definitions (version TEXT PRIMARY KEY NOT NULL,generation TEXT NOT NULL REFERENCES rec_group_generations(generation),group_id TEXT NOT NULL REFERENCES rec_group_identities(group_id),parent_version TEXT REFERENCES rec_group_definitions(version),payload TEXT NOT NULL,UNIQUE(generation,group_id))"""),
    'rec_policy_bases': (
        'base_version',
        ('base_version', 'configuration', 'byte_size', 'created_at', 'served',),
        """CREATE TABLE rec_policy_bases (base_version TEXT PRIMARY KEY NOT NULL,configuration TEXT NOT NULL,byte_size INTEGER NOT NULL CHECK(byte_size>=0),created_at REAL NOT NULL,served INTEGER NOT NULL DEFAULT 0 CHECK(served IN (0,1)))"""),
    'rec_policy_variants': (
        'model_version',
        ('model_version', 'base_version', 'served',),
        """CREATE TABLE rec_policy_variants (model_version TEXT PRIMARY KEY NOT NULL REFERENCES rec_serving_policies(model_version) ON DELETE CASCADE,base_version TEXT NOT NULL REFERENCES rec_policy_bases(base_version),served INTEGER NOT NULL DEFAULT 0 CHECK(served IN (0,1)))"""),
}


def statements():
    yield ('CREATE TABLE mood_events (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'created_at REAL NOT NULL,direction INTEGER NOT NULL)')
    yield 'CREATE INDEX ix_mood_event_order ON mood_events(created_at,id)'
    yield ('CREATE TABLE mood_event_recordings (mood_event_id INTEGER NOT NULL '
           'REFERENCES mood_events(id) ON DELETE CASCADE,recording_id INTEGER NOT NULL '
           'REFERENCES recordings(id),position INTEGER NOT NULL CHECK(position>=0),'
           'PRIMARY KEY(mood_event_id,recording_id),UNIQUE(mood_event_id,position)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_mood_seed_recording ON mood_event_recordings(recording_id,mood_event_id)'
    yield ('CREATE TABLE mood_event_unresolved_seeds (mood_event_id INTEGER NOT NULL '
           'REFERENCES mood_events(id) ON DELETE CASCADE,legacy_key TEXT NOT NULL,'
           'position INTEGER NOT NULL CHECK(position>=0),reason TEXT NOT NULL '
           "CHECK(reason IN ('unknown_recording','ambiguous_alias')),"
           'PRIMARY KEY(mood_event_id,legacy_key),UNIQUE(mood_event_id,position)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_mood_unresolved_key ON mood_event_unresolved_seeds(legacy_key,mood_event_id)'
    for table, other in (('mood_event_recordings', 'mood_event_unresolved_seeds'),
                         ('mood_event_unresolved_seeds', 'mood_event_recordings')):
        for operation in ('INSERT', 'UPDATE'):
            yield (f'CREATE TRIGGER {table}_position_{operation.lower()} BEFORE {operation} ON {table} '
                   f'WHEN EXISTS(SELECT 1 FROM {other} WHERE mood_event_id=NEW.mood_event_id '
                   'AND position=NEW.position) '
                   "BEGIN SELECT RAISE(ABORT,'Mood seed position is already occupied'); END")


def _resolve(conn, key):
    direct = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    candidates = [direct[0]] if direct else [row[0] for row in conn.execute(
        'SELECT recording_id FROM recording_aliases WHERE alias=?', (key,))]
    resolved = set()
    for recording_id in candidates:
        seen = set()
        while recording_id not in seen:
            seen.add(recording_id)
            target = conn.execute('SELECT recording_id FROM recording_redirects WHERE retired_id=?',
                                  (recording_id,)).fetchone()
            if target is None:
                resolved.add(recording_id)
                break
            recording_id = target[0]
        else:
            raise MigrationError('Cyclic recording redirect in mood seed; original event retained')
    if len(resolved) == 1:
        return resolved.pop(), None
    return None, 'ambiguous_alias' if resolved else 'unknown_recording'


def _preflight(conn):
    from . import current

    expected = current._schema_contract(sql for _, _, sql in TABLES.values())
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    affected = []
    for name, (key, columns, sql) in TABLES.items():
        if name not in present:
            if name == 'stale_dismissed':
                continue
            raise MigrationError(f'Missing key owner: {name}')
        actual = tuple(row[1] for row in conn.execute(f'PRAGMA table_xinfo("{name}")'))
        if actual != columns:
            raise MigrationError(f'Unrecognized columns in {name}; original rows retained for recovery')
        original_sql = conn.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)).fetchone()[0]
        observed = current._table(conn, name, original_sql)
        column = observed[0][key]
        observed[0][key] = (column[0], 1, *column[2:])
        if observed != expected[name][1]:
            raise MigrationError(f'Unrecognized constraints in {name}; original schema retained for recovery')
        handle = f'"{key}"' if 'WITHOUT ROWID' in sql else 'rowid'
        invalid = [row[0] for row in conn.execute(
            f'SELECT {handle} FROM "{name}" WHERE "{key}" IS NULL ORDER BY {handle} LIMIT 10')]
        if invalid:
            raise MigrationError(f'NULL logical key {name}.{key} at rowids {invalid}; '
                                 'original rows retained for recovery; no constrained cutover performed')
        affected.append(name)
    events = []
    for event_id, created_at, direction, encoded in conn.execute(
            'SELECT rowid,created_at,direction,keys FROM rec_mood ORDER BY rowid'):
        try:
            keys = json.loads(encoded)
        except (TypeError, ValueError):
            keys = None
        if created_at is None or direction is None or not isinstance(keys, list) or any(
                not isinstance(key, str) for key in keys):
            raise MigrationError(f'Invalid mood event {event_id}; original event retained for recovery')
        seeds = []
        seen = set()
        for position, key in enumerate(keys):
            recording_id, reason = _resolve(conn, key)
            identity = ('recording', recording_id) if recording_id is not None else ('unresolved', key)
            if identity not in seen:
                seeds.append((recording_id, key, position, reason))
                seen.add(identity)
        events.append((event_id, created_at, direction, seeds))
    return affected, events


def apply(conn, context):
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise MigrationError('Explicit-key replacement requires the migration runner')
    affected, events = _preflight(conn)
    # Remove dependent SQL while table names are temporarily absent. Keep its
    # exact definitions, including installed extensions, and restore it in the
    # same transaction. FK actions are disabled, so unrelated children survive.
    dependent = list(conn.execute("SELECT type,name,sql FROM sqlite_master "
                                  "WHERE type IN ('view','trigger') ORDER BY rowid"))
    if any('rec_mood' in sql.lower() for _, _, sql in dependent):
        raise MigrationError('Installed SQL depends on legacy mood fields; original schema retained')
    for kind, name, _ in reversed(dependent):
        quoted = name.replace('"', '""')
        conn.execute(f'DROP {kind} "{quoted}"')
    for name in affected:
        key, columns, sql = TABLES[name]
        temporary = f'key_replacement_{name}'
        indexes = [row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (name,))]
        conn.execute(sql.replace(f'CREATE TABLE {name}', f'CREATE TABLE "{temporary}"', 1))
        fields = ','.join(f'"{column}"' for column in columns)
        if 'WITHOUT ROWID' not in sql:
            fields = 'rowid,' + fields
        handle = f'"{key}"' if 'WITHOUT ROWID' in sql else 'rowid'
        conn.execute(f'INSERT INTO "{temporary}"({fields}) SELECT {fields} FROM "{name}"')
        different = ' OR '.join(f'old."{column}" IS NOT new."{column}"' for column in columns)
        if (conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] !=
                conn.execute(f'SELECT count(*) FROM "{temporary}"').fetchone()[0] or conn.execute(
                    f'SELECT 1 FROM "{name}" old LEFT JOIN "{temporary}" new ON new.{handle}=old.{handle} '
                    f'WHERE new.{handle} IS NULL OR {different} LIMIT 1').fetchone()):
            raise MigrationError(f'Key replacement changed original rows: {name}')
        conn.execute(f'DROP TABLE "{name}"')
        conn.execute(f'ALTER TABLE "{temporary}" RENAME TO "{name}"')
        for index in indexes:
            conn.execute(index)
    for sql in statements():
        conn.execute(sql)
    for event_id, created_at, direction, seeds in events:
        conn.execute('INSERT INTO mood_events(id,created_at,direction) VALUES (?,?,?)',
                     (event_id, created_at, direction))
        for recording_id, key, position, reason in seeds:
            if recording_id is None:
                conn.execute('INSERT INTO mood_event_unresolved_seeds VALUES (?,?,?,?)',
                             (event_id, key, position, reason))
            else:
                conn.execute('INSERT INTO mood_event_recordings VALUES (?,?,?)',
                             (event_id, recording_id, position))
    # Legacy migration receipts use the original rowid as source_id. Existing
    # events keep that exact ID; new events must not reuse archived receipt IDs
    # when all recent events have expired.
    archived_id = conn.execute(
        "SELECT max(CAST(source_id AS INTEGER)) FROM identity_state_archive "
        "WHERE kind='rec_mood' AND source_id=CAST(CAST(source_id AS INTEGER) AS TEXT)").fetchone()[0]
    if archived_id is not None:
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='mood_events'").fetchone()
        if sequence:
            conn.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='mood_events'", (archived_id,))
        else:
            conn.execute("INSERT INTO sqlite_sequence VALUES ('mood_events',?)", (archived_id,))
    conn.execute('DROP TABLE rec_mood')
    for _, _, sql in dependent:
        conn.execute(sql)


def validate(conn, *, replaced=frozenset(), foreign_keys=True, data=True):
    """Validate this replacement when v66 becomes an installed predecessor."""
    from . import current
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    definitions = [sql for name, (_, _, sql) in TABLES.items()
                   if name not in replaced and (name != 'stale_dismissed' or name in present)]
    current._validate(conn, current._schema_contract((*definitions, *statements())), 66,
                      foreign_keys=foreign_keys, data=data)
    if 'rec_mood' in present:
        raise MigrationError('Superseded mood store remains')

