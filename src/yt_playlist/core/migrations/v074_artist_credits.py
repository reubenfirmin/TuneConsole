"""Share ordered credit content without inventing historical delivery identity.

Legacy timestamps do not establish provider revisions. Every old observation
keeps its ID, source and timestamp, even when its document repeats. Current
projections receive an explicitly unknown-time selection snapshot: equal
content does not establish which historical capture supplied the selection.
"""
import json

from yt_playlist.core import artist_credits as credits
from yt_playlist.core.activity import check_cancellable_work
from .runner import MigrationError


def _finite(name):
    return f"CHECK({name} IS NULL OR (typeof({name}) IN ('integer','real') AND {name} BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308))"


def statements():
    yield ('CREATE TABLE artist_credits (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'display_credit TEXT NOT NULL,content_digest TEXT NOT NULL UNIQUE '
           "CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),"
           "member_count INTEGER NOT NULL CHECK(typeof(member_count)='integer' AND member_count BETWEEN 1 AND 128),"
           "complete INTEGER NOT NULL DEFAULT 0 CHECK(typeof(complete)='integer' AND complete IN (0,1)))")
    yield ('CREATE TABLE artist_credit_members (credit_id INTEGER NOT NULL REFERENCES artist_credits(id),'
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 127),"
           'artist_id INTEGER REFERENCES artists(id),credit_text TEXT NOT NULL CHECK(length(credit_text)>0),'
           'provider TEXT,raw_id TEXT,raw_browse_id TEXT,role TEXT,'
           "presence INTEGER NOT NULL CHECK(typeof(presence)='integer' AND presence BETWEEN 0 AND 7),"
           'CHECK((provider IS NULL)=(COALESCE(NULLIF(raw_id,\'\'),NULLIF(raw_browse_id,\'\')) IS NULL)),'
           'CHECK((presence&1)<>0 OR raw_id IS NULL),CHECK((presence&2)<>0 OR raw_browse_id IS NULL),'
           'CHECK((presence&4)<>0 OR role IS NULL),PRIMARY KEY(credit_id,position)) WITHOUT ROWID')
    yield ('CREATE INDEX ix_credit_member_artist ON artist_credit_members(artist_id,credit_id) '
           'WHERE artist_id IS NOT NULL')
    yield ('CREATE TABLE artist_credit_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),'
           'credit_id INTEGER NOT NULL REFERENCES artist_credits(id),'
           'source TEXT NOT NULL CHECK(length(source)>0),delivery_key TEXT '
           'CHECK(delivery_key IS NULL OR length(delivery_key)>0),first_seen REAL ' + _finite('first_seen') + ','
           'last_seen REAL ' + _finite('last_seen') + ','
           "processing_count INTEGER NOT NULL CHECK(typeof(processing_count)='integer' AND processing_count>0),"
           "capture_kind TEXT NOT NULL CHECK(capture_kind IN ('legacy_capture','selection_snapshot',"
           "'capture','provider_delivery','identity_reconciliation')),"
           'CHECK((first_seen IS NULL AND last_seen IS NULL) OR '
           '(first_seen IS NOT NULL AND last_seen IS NOT NULL AND first_seen<=last_seen)),'
           "CHECK((capture_kind IN ('provider_delivery','identity_reconciliation'))=(delivery_key IS NOT NULL)))")
    yield ('CREATE UNIQUE INDEX ix_credit_delivery ON artist_credit_observations(entry_id,source,delivery_key) '
           'WHERE delivery_key IS NOT NULL')
    yield 'CREATE INDEX ix_credit_entry_source ON artist_credit_observations(entry_id,source,id)'
    yield 'CREATE INDEX ix_credit_observation_content ON artist_credit_observations(credit_id,id)'
    yield ('CREATE TABLE artist_credit_capture_heads (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),'
           'source TEXT NOT NULL,observation_id INTEGER NOT NULL REFERENCES artist_credit_observations(id),'
           'PRIMARY KEY(entry_id,source)) WITHOUT ROWID')
    yield ('CREATE INDEX ix_credit_capture_observation ON artist_credit_capture_heads(observation_id)')
    yield ('CREATE TABLE entry_artist_selections (entry_id INTEGER PRIMARY KEY NOT NULL REFERENCES catalog_entries(id),'
           'observation_id INTEGER NOT NULL REFERENCES artist_credit_observations(id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_entry_artist_selection_observation ON entry_artist_selections(observation_id)'
    yield ('CREATE TRIGGER immutable_artist_credit BEFORE UPDATE OF id,display_credit,content_digest,member_count '
           "ON artist_credits BEGIN SELECT RAISE(ABORT,'Artist credit content is immutable'); END")
    yield ('CREATE TRIGGER private_artist_credit BEFORE INSERT ON artist_credits WHEN NEW.complete<>0 '
           "BEGIN SELECT RAISE(ABORT,'Artist credit must be assembled privately'); END")
    yield ('CREATE TRIGGER complete_artist_credit BEFORE UPDATE OF complete ON artist_credits WHEN '
           'OLD.complete<>0 OR NEW.complete<>1 OR OLD.member_count<>(SELECT count(*) FROM artist_credit_members '
           'WHERE credit_id=OLD.id) OR (SELECT min(position) FROM artist_credit_members WHERE credit_id=OLD.id)<>0 '
           'OR (SELECT max(position) FROM artist_credit_members WHERE credit_id=OLD.id)<>OLD.member_count-1 '
           "BEGIN SELECT RAISE(ABORT,'Artist credit membership is incomplete or sealed'); END")
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
        retained = (' AND EXISTS(SELECT 1 FROM artist_credit_observations o WHERE o.credit_id=c.id)'
                    if event == 'DELETE' else '')
        condition = ' OR '.join('EXISTS(SELECT 1 FROM artist_credits c WHERE c.id=' + side
                                + '.credit_id AND c.complete=1' + retained + ')' for side in sides)
        yield (f'CREATE TRIGGER immutable_artist_credit_member_{event.lower()} BEFORE {event} '
               f'ON artist_credit_members WHEN {condition} '
               "BEGIN SELECT RAISE(ABORT,'Artist credit members are immutable'); END")
    yield ('CREATE TRIGGER complete_credit_observation BEFORE INSERT ON artist_credit_observations WHEN '
           'NOT EXISTS(SELECT 1 FROM artist_credits WHERE id=NEW.credit_id AND complete=1) '
           "BEGIN SELECT RAISE(ABORT,'Artist evidence requires complete credit content'); END")
    yield ('CREATE TRIGGER immutable_credit_observation BEFORE UPDATE OF '
           'id,entry_id,credit_id,source,delivery_key,capture_kind ON artist_credit_observations '
           "BEGIN SELECT RAISE(ABORT,'Artist credit evidence identity is immutable'); END")
    yield ('CREATE TRIGGER monotonic_credit_processing BEFORE UPDATE OF first_seen,last_seen,processing_count '
           'ON artist_credit_observations WHEN '
           "OLD.capture_kind IN ('legacy_capture','selection_snapshot') OR NEW.processing_count<>OLD.processing_count+1 "
           'OR (OLD.first_seen IS NOT NULL AND (NEW.first_seen IS NULL OR NEW.first_seen>OLD.first_seen)) '
           'OR (OLD.last_seen IS NOT NULL AND (NEW.last_seen IS NULL OR NEW.last_seen<OLD.last_seen)) '
           "BEGIN SELECT RAISE(ABORT,'Artist processing history must advance without rewriting captures'); END")
    for table, same_source in (('artist_credit_capture_heads', True), ('entry_artist_selections', False)):
        for event in ('INSERT', 'UPDATE'):
            yield (f'CREATE TRIGGER owned_{table}_{event.lower()} BEFORE {event} ON {table} '
                   'WHEN NOT EXISTS(SELECT 1 FROM artist_credit_observations o '
                   'WHERE o.id=NEW.observation_id AND o.entry_id=NEW.entry_id'
                   + (' AND o.source=NEW.source' if same_source else '') + ') '
                   "BEGIN SELECT RAISE(ABORT,'Selected artist evidence has a different owner'); END")


def _documents(conn):
    return conn.execute('SELECT DISTINCT credit,credits_json,source FROM artist_credit_observations')


def _prepare(conn, credit, raw, source):
    if not isinstance(raw, str) or len(raw.encode()) > credits.MAX_BYTES:
        raise ValueError('Artist source credit exceeds its content budget')
    return credits.prepare(conn, credit, json.loads(raw), source)


def _preflight(conn):
    try:
        for row in _documents(conn):
            check_cancellable_work()
            _prepare(conn, *row)
        if conn.execute("SELECT 1 FROM artist_credit_observations WHERE observed_at IS NOT NULL AND "
                        "(typeof(observed_at) NOT IN ('integer','real') OR "
                        'observed_at NOT BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308) LIMIT 1').fetchone():
            raise ValueError('Nonfinite or untyped artist observation time')
        if conn.execute('SELECT 1 FROM track_artists GROUP BY entry_id '
                        'HAVING min(position)<>0 OR max(position)<>count(*)-1 OR count(*)>128 LIMIT 1').fetchone():
            raise ValueError('Noncontiguous current artist positions require explicit recovery')
        if conn.execute("SELECT 1 FROM track_artists WHERE typeof(position)<>'integer' "
                        "OR typeof(credit)<>'text' OR length(credit)=0 OR typeof(source)<>'text' OR length(source)=0 LIMIT 1").fetchone():
            raise ValueError('Invalid current artist credit fields')
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise MigrationError(f'Invalid artist credit evidence: {exc}; original rows retained') from exc


def _selections(conn):
    after = None
    while True:
        entries = conn.execute('SELECT DISTINCT entry_id FROM track_artists'
                               + (' WHERE entry_id>?' if after is not None else '')
                               + ' ORDER BY entry_id LIMIT 128', (after,) if after is not None else ()).fetchall()
        if not entries:
            return
        for entry, in entries:
            check_cancellable_work()
            rows = conn.execute('SELECT artist_id,credit,source FROM track_artists '
                                'WHERE entry_id=? ORDER BY position', (entry,)).fetchall()
            # Selected identities are established by this current projection.
            # There was no selected-observation FK, so even a complete content
            # match cannot prove that one particular old capture supplied it.
            display = conn.execute('SELECT artist FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] or ''
            credit = credits.Credit(display, tuple(credits.Member(row['artist_id'], row['credit']) for row in rows))
            version = credits.intern(conn, credit)
            sources = {row['source'] for row in rows}
            source = next(iter(sources)) if len(sources) == 1 else 'legacy_selected_sources'
            observation = conn.execute('INSERT INTO artist_credit_observations('
                'entry_id,credit_id,source,processing_count,capture_kind) VALUES (?,?,?,1,\'selection_snapshot\')',
                (entry, version, source)).lastrowid
            conn.execute('INSERT INTO entry_artist_selections VALUES (?,?)', (entry, observation))
        after = entries[-1][0]


def apply(conn, context):
    _preflight(conn)
    # This temporary join map bounds Python memory to one source document. No
    # library-sized dictionary of raw JSON is retained by the migration.
    conn.execute('CREATE TEMP TABLE artist_credit_import_map('
                 'credit TEXT NOT NULL,raw TEXT NOT NULL,source TEXT NOT NULL,credit_id INTEGER NOT NULL,'
                 'PRIMARY KEY(credit,raw,source)) WITHOUT ROWID')
    try:
        conn.execute('ALTER TABLE artist_credit_observations RENAME TO legacy_artist_credit_observations')
        for sql in statements():
            conn.execute(sql)
        for display, raw, source in conn.execute('SELECT DISTINCT credit,credits_json,source '
                                                 'FROM legacy_artist_credit_observations'):
            check_cancellable_work()
            version = credits.intern(conn, _prepare(conn, display, raw, source))
            conn.execute('INSERT INTO artist_credit_import_map VALUES (?,?,?,?)', (display, raw, source, version))
        conn.execute('INSERT INTO artist_credit_observations('
            'id,entry_id,credit_id,source,first_seen,last_seen,processing_count,capture_kind) '
            "SELECT o.id,o.entry_id,m.credit_id,o.source,o.observed_at,o.observed_at,1,'legacy_capture' "
            'FROM legacy_artist_credit_observations o JOIN artist_credit_import_map m '
            'ON m.credit=o.credit AND m.raw=o.credits_json AND m.source=o.source')
        # Legacy rows remain separate, with no fabricated delivery key. Runtime
        # captures start their own head on first processing after the upgrade.
        _selections(conn)
        conn.execute('DROP TABLE legacy_artist_credit_observations')
    finally:
        conn.execute('DROP TABLE IF EXISTS temp.artist_credit_import_map')


def validate(conn):
    from .current import _tokens
    for sql in statements():
        words = sql.split()
        name = words[3] if words[1] == 'UNIQUE' else words[2]
        actual = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible normalized artist credits: {name}')
    if conn.execute('SELECT 1 FROM track_artists t WHERE NOT EXISTS('
        'SELECT 1 FROM entry_artist_selections s WHERE s.entry_id=t.entry_id) LIMIT 1').fetchone():
        raise MigrationError('Selected artist projection has no evidence owner')
