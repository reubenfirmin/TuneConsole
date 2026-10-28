"""Versioned current bootstrap and schema contract, independent of the baseline.

Recognized unversioned databases and every numbered predecessor remain supported
through the frozen historical upgrade path. Empty databases start directly at
VERSION. A later schema replacement must publish its own current definition.
"""
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
import re
import sqlite3

from .runner import MigrationError


CONTRACT_FLOOR = 32
VERSION = 51
DEFINITION = Path(__file__).with_name('schema_v033.sql')
SEED_SETTINGS = {
    'catalog_identity_version': '2',
    'discovery_enrichment_audit_requeued': '1',
    'enrich_ts_backfilled': '1',
    'enrich_unattempted_batch_repaired': '1',
    'like_ratchet_repaired': 'adjusted 0 of 0 like-touched axis weight(s); 0 like row(s) stamped '
                            'sync-provenance; 0 once-ever graduation stamp(s) backfilled',
    'playlist_created_at_backfilled': '1',
}
TOKEN = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                   r"`(?:``|[^`])*`|\[[^]]*\]|[A-Za-z_]\w*|\S", re.S)


def statements(version=VERSION):
    if version == 51:
        from .v051_listening_actions import statements as listening_statements
        yield from statements(50)
        yield from listening_statements()
        return
    if version == 50:
        from .v050_musical_profiles import statements as musical_statements
        yield from statements(49)
        yield from musical_statements()
        return
    if version == 49:
        from .v049_metadata_observations import statements as observation_statements
        yield from statements(48)
        yield from observation_statements()
        return
    if version == 48:
        from .v048_playback_alternates import statements as alternate_statements
        yield from statements(47)
        yield from alternate_statements()
        return
    if version == 47:
        from .v047_playlist_revisions import statements as playlist_statements
        yield from statements(46)
        yield from playlist_statements()
        return
    if version in (43, 46):  # These migrations only retire obsolete setting overrides.
        yield from statements(version - 1)
        return
    if version == 45:
        yield from statements(44)
        yield "CREATE INDEX ix_actions_delete_pending ON actions(id DESC) WHERE status='delete_pending'"
        return
    if version == 44:
        from .v044_prepared_content import statements as content_statements
        yield from statements(43)
        yield from content_statements()
        return
    if version == 42:
        from .v042_foreign_calendar_scopes import statements as foreign_calendar_statements
        yield from statements(41)
        yield from foreign_calendar_statements()
        return
    if version == 41:
        from .v041_scorecard_lookups import statements as scorecard_statements
        yield from statements(40)
        yield from scorecard_statements()
        return
    if version == 40:
        from .v040_player_refresh_inputs import statements as player_statements
        yield from statements(39)
        yield from player_statements()
        return
    if version == 39:
        from .v039_background_refresh import statements as refresh_statements
        yield from statements(38)
        yield from refresh_statements()
        return
    if version == 38:
        from .v038_listening_day_counts import statements as count_statements
        yield from statements(37)
        yield from count_statements()
        return
    if version == 37:
        from .v037_home_query_plans import statements as home_statements
        yield from statements(36)
        yield from home_statements()
        return
    if version == 36:
        from .v036_progressive_signals import statements as signal_statements
        yield from statements(35)
        yield from signal_statements()
        return
    if version == 35:
        from .v035_progressive_source_members import statements as source_statements
        yield from statements(34)
        yield from source_statements()
        return
    if version == 34:
        from .v034_progressive_learning import statements as progressive_statements
        yield from statements(33)
        yield from progressive_statements()
        return
    pending = ''
    definition = DEFINITION if version == VERSION else DEFINITION.with_name(f'schema_v{version:03}.sql')
    for line in definition.read_text().splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete current schema definition')


def _tokens(sql):
    result = []
    for match in TOKEN.finditer(sql or ''):
        token = match.group()
        if token.startswith(('--', '/*')):
            continue
        if token.startswith("'"):
            result.append(token)
        elif token.startswith(('"', '`', '[')):
            result.append(token[1:-1].replace(token[0] * 2, token[0]).lower())
        else:
            result.append(token.lower())
    return tuple(result)


def _checks(sql):
    tokens = _tokens(sql)
    checks = []
    for start, token in enumerate(tokens[:-1]):
        if token != 'check' or tokens[start + 1] != '(':
            continue
        depth = 0
        for end in range(start + 1, len(tokens)):
            depth += (tokens[end] == '(') - (tokens[end] == ')')
            if depth == 0:
                checks.append(tokens[start + 2:end])
                break
    return Counter(checks)


def _table_clauses(sql):
    """Split column/table constraints without splitting composite keys or expressions."""
    tokens = _tokens(sql)
    start = tokens.index('(') + 1
    depth = 0
    for position in range(start, len(tokens)):
        token = tokens[position]
        if depth == 0 and token in (',', ')'):
            yield tokens[start:position]
            start = position + 1
            if token == ')':
                return
        depth += (token == '(') - (token == ')')


def _foreign_key_deferrals(sql):
    """SQLite's FK pragma omits deferral; retain it on its actual owning key."""
    result = Counter()
    for clause in _table_clauses(sql):
        depth = 0
        top_level = []
        for position, token in enumerate(clause):
            if depth == 0:
                top_level.append(position)
            depth += (token == '(') - (token == ')')
        reference = next((p for p in top_level if clause[p] == 'references'), None)
        if reference is None:
            continue
        declaration = clause[2:] if clause[0] == 'constraint' else clause
        if declaration[:2] == ('foreign', 'key'):
            children = tuple(t for t in declaration[3:declaration.index(')')] if t != ',')
        else:
            children = (clause[0],)
        parent = clause[reference + 1]
        parent_columns = ()
        if clause[reference + 2:reference + 3] == ('(',):
            end = clause.index(')', reference + 3)
            parent_columns = tuple(t for t in clause[reference + 3:end] if t != ',')
        deferral = ()
        for position in top_level:
            if position > reference and clause[position] == 'deferrable':
                start = position - (clause[position - 1] == 'not')
                end = position + (3 if clause[position + 1:position + 2] == ('initially',) else 1)
                deferral = clause[start:end]
        result[(children, parent, parent_columns, deferral)] += 1
    return result


def _table(conn, name, sql):
    quoted = '"' + name.replace('"', '""') + '"'
    columns = {r[1]: (r[2].upper(), r[3], r[4], r[5], r[6]) for r in conn.execute(f'PRAGMA table_xinfo({quoted})')}
    grouped_keys = defaultdict(list)
    for row in conn.execute(f'PRAGMA foreign_key_list({quoted})'):
        grouped_keys[row[0]].append(row)
    foreign_keys = Counter(tuple(tuple(r[2:]) for r in sorted(rows, key=lambda r: r[1]))
                           for rows in grouped_keys.values())
    unique = Counter()
    for index in conn.execute(f'PRAGMA index_list({quoted})'):
        if index[3] in ('u', 'pk'):
            index_name = '"' + index[1].replace('"', '""') + '"'
            unique[(index[3], tuple((r[2], r[3], r[4]) for r in conn.execute(
                f'PRAGMA index_xinfo({index_name})') if r[5]))] += 1
    flags = tuple(conn.execute('SELECT wr,strict FROM pragma_table_list WHERE schema=? AND name=?',
                               ('main', name)).fetchone())
    tokens = _tokens(sql)
    annotations = Counter()
    for position, token in enumerate(tokens):
        if token == 'collate':
            annotations[tokens[position:position + 2]] += 1
        elif token == 'autoincrement':
            annotations[(token,)] += 1
    return columns, foreign_keys, unique, _checks(sql), flags, annotations, _foreign_key_deferrals(sql)


@lru_cache(maxsize=2)
def contract(version=VERSION):
    reference = sqlite3.connect(':memory:')
    try:
        for sql in statements(version):
            reference.execute(sql)
        return {
            name: (kind, _table(reference, name, sql) if kind == 'table' else _tokens(sql))
            for name, kind, sql in reference.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
        }
    finally:
        reference.close()


def bootstrap(conn, registry, now):
    """Initialize the complete current state in the runner's single transaction."""
    if len(registry) < VERSION:
        raise MigrationError('Current bootstrap is ahead of the migration registry')
    for sql in statements():
        conn.execute(sql)
    conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', SEED_SETTINGS.items())
    conn.executemany('INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)',
                     [(m.version, m.name, now) for m in registry[:VERSION]])
    validate(conn)


def validate(conn, version=VERSION, *, replaced=frozenset()):
    objects = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    if version == VERSION and 'album_primary_provider_ids' in objects:
        from . import v062_album_aliases
        if not replaced:
            v062_album_aliases.validate(conn)
        replaced = set(replaced) | v062_album_aliases.REPLACED
    for name, (kind, expected) in contract(version).items():
        if name in replaced:
            continue
        actual = objects.get(name)
        if actual is None or actual[0] != kind:
            raise MigrationError(f'Missing current {kind}: {name}')
        if kind == 'table':
            observed = _table(conn, name, actual[1])
            columns, *constraints = expected
            if any(observed[0].get(key) != value for key, value in columns.items()) or observed[1:] != tuple(constraints):
                raise MigrationError(f'Incompatible current table/constraint: {name}')
        elif _tokens(actual[1]) != expected:
            raise MigrationError(f'Incompatible current {kind}: {name}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid current foreign-key reference')
    for key in SEED_SETTINGS:
        if conn.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone() is None:
            raise MigrationError(f'Missing initialized schema setting: {key}')
    identity_version = conn.execute("SELECT value FROM settings WHERE key='catalog_identity_version'").fetchone()
    if identity_version[0] != '2':
        raise MigrationError('Incompatible initialized catalog identity version')
    _validate_data(conn)
    if version >= 33:
        for name in ('history_items', 'history_snapshots', 'legacy_listening_observations'):
            if name in objects:
                raise MigrationError(f'Retired capture structure remains: {name}')
        if conn.execute('SELECT 1 FROM listening_observations o JOIN listening_capture_receipts c '
                        'ON c.id=o.capture_id WHERE o.identity_id<>c.identity_id '
                        'OR o.first_observed_at IS NOT c.captured_at OR o.last_observed_at IS NOT c.captured_at '
                        "OR o.source_day IS NOT date(c.captured_at,'unixepoch') "
                        "OR o.day IS NOT date(c.captured_at,'unixepoch') LIMIT 1").fetchone():
            raise MigrationError('Historical capture receipt does not match its observation')


def _validate_data(conn):
    checks = (
        ('Invalid generated-play policy',
         'SELECT 1 FROM play_events WHERE is_generated IS NOT NULL AND is_generated NOT IN (0,1) LIMIT 1'),
        ('Missing exact-event calendar projection',
         'SELECT 1 FROM play_events pe LEFT JOIN play_event_calendar c ON c.event_id=pe.id WHERE c.event_id IS NULL LIMIT 1'),
        ('Missing exact-event source contribution',
         'SELECT 1 FROM play_events pe WHERE NOT EXISTS (SELECT 1 FROM play_event_sources s WHERE s.event_id=pe.id) LIMIT 1'),
        ('Invalid retained playlist membership',
         'SELECT 1 FROM playlists p LEFT JOIN playlist_membership_refs m ON m.id=p.id '
         'WHERE m.id IS NULL OR m.scope_ref IS NOT p.scope_ref OR m.identity_id IS NOT p.identity_id LIMIT 1'),
        ('Invalid playlist scope ownership',
         'SELECT 1 FROM playlists p WHERE NOT EXISTS (SELECT 1 FROM playlist_scopes s WHERE s.ref=p.scope_ref '
         'AND s.provider_id=p.ytm_playlist_id AND ((s.kind=\'account\' AND s.identity_id=p.identity_id '
         "AND s.provider_id IN ('LM','SE')) OR (s.kind='shared' AND s.identity_id IS NULL "
         "AND s.provider_id NOT IN ('LM','SE')))) LIMIT 1"),
        ('Colliding playlist positions',
         'SELECT 1 FROM playlist_tracks GROUP BY playlist_id,position HAVING COUNT(*)>1 LIMIT 1'),
        ('Invalid playlist position',
         "SELECT 1 FROM playlist_tracks WHERE typeof(position)<>'integer' OR position<0 LIMIT 1"),
        ('Discovery representative belongs to another recording',
         'SELECT 1 FROM discovery_memberships d JOIN catalog_entries e ON e.id=d.entry_id '
         'WHERE d.recording_id<>e.recording_id LIMIT 1'),
        ('Artist redirects must point directly to canonical identities',
         'SELECT 1 FROM artist_redirects r JOIN artist_redirects target ON target.retired_id=r.artist_id LIMIT 1'),
        ('Artist redirect requires a provisional source and canonical target',
         'SELECT 1 FROM artist_redirects r JOIN artists old ON old.id=r.retired_id '
         'JOIN artists target ON target.id=r.artist_id WHERE old.provisional<>1 OR target.provisional<>0 '
         'OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.retired_id) '
         'OR NOT EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.artist_id) LIMIT 1'),
    )
    for message, sql in checks:
        if conn.execute(sql).fetchone():
            raise MigrationError(message)
    for table in ('artist_scans', 'discovered_artists', 'rec_artist_similar'):
        if conn.execute(f'SELECT 1 FROM {table} WHERE artist_id IS NULL LIMIT 1').fetchone():
            raise MigrationError(f'Unresolved artist state: {table}')
