"""Displayed mix descriptors, independent of track membership and group history."""
import hashlib

from .evidence_ledger import canonical
from .mix_choice_policy import PRECISION, GROUP_FIELDS, visible_option, context, menu_plan, project as project_record


def definitions():
    yield 'mix_choice_raw_genres', 'CREATE TABLE mix_choice_raw_genres (id INTEGER PRIMARY KEY,label TEXT NOT NULL UNIQUE)'
    yield 'mix_choice_genre_sets', 'CREATE TABLE mix_choice_genre_sets (id INTEGER PRIMARY KEY,digest BLOB NOT NULL UNIQUE CHECK(length(digest)=32))'
    yield 'mix_choice_genre_members', '''CREATE TABLE mix_choice_genre_members (
        set_id INTEGER NOT NULL REFERENCES mix_choice_genre_sets(id),
        genre_id INTEGER NOT NULL REFERENCES mix_choice_raw_genres(id),PRIMARY KEY(set_id,genre_id)) WITHOUT ROWID'''
    yield 'mix_choice_themes', '''CREATE TABLE mix_choice_themes (
        id INTEGER PRIMARY KEY,intent TEXT NOT NULL,label TEXT NOT NULL,note TEXT NOT NULL,UNIQUE(intent,label,note))'''
    yield 'mix_choice_descriptors', '''CREATE TABLE mix_choice_descriptors (
        id INTEGER PRIMARY KEY,theme_id INTEGER NOT NULL REFERENCES mix_choice_themes(id),
        genre_set_id INTEGER NOT NULL REFERENCES mix_choice_genre_sets(id),UNIQUE(theme_id,genre_set_id))'''
    yield 'mix_choice_menus', '''CREATE TABLE mix_choice_menus (
        opportunity_id TEXT PRIMARY KEY NOT NULL,context TEXT NOT NULL,source_context TEXT NOT NULL,
        source_digest BLOB NOT NULL CHECK(length(source_digest)=32),context_at REAL NOT NULL) WITHOUT ROWID'''
    yield 'mix_choice_options', '''CREATE TABLE mix_choice_options (
        opportunity_id TEXT NOT NULL REFERENCES mix_choice_menus(opportunity_id) ON DELETE CASCADE,
        option_id TEXT NOT NULL,position INTEGER NOT NULL CHECK(position>0),
        descriptor_id INTEGER NOT NULL REFERENCES mix_choice_descriptors(id),
        genres_complete INTEGER NOT NULL CHECK(genres_complete IN (0,1)),
        origin TEXT NOT NULL CHECK(origin IN ('rendered','legacy_tracks')),
        PRIMARY KEY(opportunity_id,option_id),UNIQUE(opportunity_id,position)) WITHOUT ROWID'''
    yield 'ix_mix_choice_descriptor', 'CREATE INDEX ix_mix_choice_descriptor ON mix_choice_options(descriptor_id)'
    yield 'choice_context_projections', '''CREATE TABLE choice_context_projections (
        source_context TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('mixes','independent')),
        context TEXT NOT NULL,source_digest BLOB NOT NULL CHECK(length(source_digest)=32),
        PRIMARY KEY(source_context,kind)) WITHOUT ROWID'''
    yield 'mix_choice_preparation', '''CREATE TABLE mix_choice_preparation (
        id INTEGER PRIMARY KEY CHECK(id=1),phase TEXT NOT NULL CHECK(phase IN ('menus','learners','displays','roots','ready')),
        cursor TEXT NOT NULL DEFAULT '')'''
    # Additive projection extension; the applied choice-learning schema stays intact.
    yield 'choice_interest_mix_descriptors', '''CREATE TABLE choice_interest_mix_descriptors (
        publication_id INTEGER NOT NULL,context_position INTEGER NOT NULL,
        descriptor_id INTEGER NOT NULL REFERENCES mix_choice_descriptors(id),
        PRIMARY KEY(publication_id,context_position),
        FOREIGN KEY(publication_id,context_position) REFERENCES choice_interest_contexts(publication_id,position)
        ON DELETE CASCADE) WITHOUT ROWID'''
    yield 'ix_choice_interest_descriptor', 'CREATE INDEX ix_choice_interest_descriptor ON choice_interest_mix_descriptors(descriptor_id)'
    yield 'prepared_choice_interest_mix_descriptors', '''CREATE TABLE prepared_choice_interest_mix_descriptors (
        publication_id INTEGER NOT NULL,context_position INTEGER NOT NULL,
        descriptor_id INTEGER NOT NULL REFERENCES mix_choice_descriptors(id),
        PRIMARY KEY(publication_id,context_position),
        FOREIGN KEY(publication_id,context_position) REFERENCES prepared_choice_interest_contexts(publication_id,position)
        ON DELETE CASCADE) WITHOUT ROWID'''
    for table in ('mix_choice_raw_genres', 'mix_choice_genre_sets', 'mix_choice_genre_members',
                  'mix_choice_themes', 'mix_choice_descriptors', 'mix_choice_menus',
                  'mix_choice_options', 'choice_context_projections'):
        name = table + '_immutable'
        yield name, (f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} BEGIN "
                     "SELECT RAISE(ABORT,'Captured mix choice is immutable'); END")
    for action, prefix in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
        name = 'mix_choice_genre_members_' + action.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {action} ON mix_choice_genre_members WHEN EXISTS('
                     f'SELECT 1 FROM mix_choice_descriptors WHERE genre_set_id={prefix}.set_id) '
                     "BEGIN SELECT RAISE(ABORT,'Captured genre membership is immutable'); END")


def available(conn):
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='mix_choice_descriptors'").fetchone())


def install(conn, context=None):
    if available(conn):
        validate(conn)
        return
    for _, sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible mix choice owner: ' + name)


def _intern(conn, table, fields, values):
    conn.execute('INSERT INTO '+table+'('+','.join(fields)+') VALUES ('+','.join('?' for _ in values)+') ON CONFLICT DO NOTHING', values)
    return conn.execute('SELECT id FROM '+table+' WHERE '+' AND '.join(f+'=?' for f in fields), values).fetchone()[0]


def descriptor(conn, option):
    return insert_descriptor(conn, visible_option(option))


def insert_descriptor(conn, option):
    if 'descriptor_id' in option:
        read_descriptor(conn, option['descriptor_id'])
        return option['descriptor_id']
    genres = tuple(option['genres'])
    digest = hashlib.sha256(canonical(genres)).digest()
    prior = conn.execute('SELECT id FROM mix_choice_genre_sets WHERE digest=?', (digest,)).fetchone()
    if prior:
        set_id = prior[0]
        members = tuple(row[0] for row in conn.execute('SELECT g.label FROM mix_choice_genre_members m '
            'JOIN mix_choice_raw_genres g ON g.id=m.genre_id WHERE m.set_id=? ORDER BY g.label', (set_id,)))
        if members != genres:
            raise ValueError('Mix genre-set digest collision')
    else:
        set_id = conn.execute('INSERT INTO mix_choice_genre_sets(digest) VALUES (?)', (digest,)).lastrowid
        for genre in genres:
            genre_id = _intern(conn, 'mix_choice_raw_genres', ('label',), (genre,))
            conn.execute('INSERT INTO mix_choice_genre_members VALUES (?,?)', (set_id, genre_id))
    theme = _intern(conn, 'mix_choice_themes', ('intent', 'label', 'note'),
                    tuple(option.get(key) or '' for key in ('intent', 'label', 'note')))
    return _intern(conn, 'mix_choice_descriptors', ('theme_id', 'genre_set_id'), (theme, set_id))


def read_descriptor(conn, identity):
    row = conn.execute('SELECT t.intent,t.label,t.note,d.genre_set_id FROM mix_choice_descriptors d '
        'JOIN mix_choice_themes t ON t.id=d.theme_id WHERE d.id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('Missing mix choice descriptor')
    genres = [r[0] for r in conn.execute('SELECT g.label FROM mix_choice_genre_members m '
        'JOIN mix_choice_raw_genres g ON g.id=m.genre_id WHERE m.set_id=? ORDER BY g.label', (row[3],))]
    return dict(zip(('intent', 'label', 'note'), row[:3])) | {'genres': genres}


def publish_menu(conn, record, context_id, *, source=None):
    """Commit descriptors before acknowledging the menu; reuse verifies its source."""
    insert_menu(conn, menu_plan(record, context_id, source=source))
    return project(conn, record)


def insert_menu(conn, plan):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    prior = conn.execute('SELECT source_digest FROM mix_choice_menus WHERE opportunity_id=?', (plan['id'],)).fetchone()
    if prior:
        if prior[0] != plan['source_digest']:
            raise ValueError('Mix menu source changed during preparation')
        return
    conn.execute('INSERT INTO mix_choice_menus VALUES (?,?,?,?,?)',
                 tuple(plan[key] for key in ('id', 'context', 'source_context', 'source_digest', 'context_at')))
    for position, option in enumerate(plan['options'], 1):
        identity = insert_descriptor(conn, option)
        complete = bool(option.get('genres_complete', True))
        origin = option.get('genre_origin', 'rendered')
        conn.execute('INSERT INTO mix_choice_options VALUES (?,?,?,?,?,?)',
                     (plan['id'], str(option['id']), position, identity, int(complete), origin))


def project(conn, record):
    """Overlay a prepared legacy menu; immutable original JSON remains until #626."""
    if not record or 'context' not in record:
        return record
    return project_record(record, projection_facts(conn, record.get('id'), record['context'],
                                                 mixes=record.get('format') == 'mixes'))


def projection_facts(conn, opportunity_id, context_id, *, mixes):
    installed = available(conn)
    facts = {'installed': installed, 'context': None, 'context_at': None, 'options': {}}
    if not installed:
        return facts
    if not mixes:
        context = conn.execute("SELECT context FROM choice_context_projections WHERE source_context=? AND kind='independent'",
                               (context_id,)).fetchone()
        return {**facts, 'context': context[0] if context else None}
    menu = conn.execute('SELECT context,context_at FROM mix_choice_menus WHERE opportunity_id=?', (opportunity_id,)).fetchone()
    if menu is None:
        return facts
    options = {row['option_id']: dict(row) for row in conn.execute('SELECT * FROM mix_choice_options '
                                                                'WHERE opportunity_id=?', (opportunity_id,))}
    return {**facts, 'context': menu[0], 'context_at': menu[1], 'options': options}


def expand(conn, option):
    return ({**read_descriptor(conn, option['descriptor_id']), **option}
            if 'descriptor_id' in option else visible_option(option))


def retire(conn, identities):
    """Called after the ledger has removed these exact menu files."""
    if available(conn):
        conn.executemany('DELETE FROM mix_choice_menus WHERE opportunity_id=?', ((i,) for i in identities))


def collect_unpublished(store, limit=128):
    """Reconcile SQL committed before a failed event write, under admission."""
    if not available(store.conn):
        return 0
    from . import group_admission
    with group_admission.admission(store):
        after = getattr(store, '_mix_choice_orphan_after', '')
        with store._lock:
            identities = [row[0] for row in store.conn.execute('SELECT opportunity_id FROM mix_choice_menus '
                'WHERE opportunity_id>? ORDER BY opportunity_id LIMIT ?', (after, limit))]
        ledger = store.rec_ledger.evidence()
        missing = [identity for identity in identities if not ledger._path('opportunity', identity).exists()]
        with group_admission.writer(store):
            retire(store.conn, missing)
            collect_dictionary(store.conn, limit)
        store._mix_choice_orphan_after = identities[-1] if len(identities) == limit else ''
        return len(missing)


def collect_dictionary(conn, limit=128):
    """One bounded dependency-first prefix, preserving live learning descriptors."""
    if not available(conn):
        return 0
    predicates = (
        ('mix_choice_descriptors', 'id', 'NOT EXISTS(SELECT 1 FROM mix_choice_options o WHERE o.descriptor_id=d.id) '
         'AND NOT EXISTS(SELECT 1 FROM choice_interest_mix_descriptors i WHERE i.descriptor_id=d.id) '
         'AND NOT EXISTS(SELECT 1 FROM prepared_choice_interest_mix_descriptors i WHERE i.descriptor_id=d.id)'),
        ('mix_choice_themes', 'id', 'NOT EXISTS(SELECT 1 FROM mix_choice_descriptors x WHERE x.theme_id=d.id)'),
        ('mix_choice_genre_members', '(set_id,genre_id)', 'NOT EXISTS(SELECT 1 FROM mix_choice_descriptors x WHERE x.genre_set_id=d.set_id)'),
        ('mix_choice_genre_sets', 'id', 'NOT EXISTS(SELECT 1 FROM mix_choice_descriptors x WHERE x.genre_set_id=d.id) '
         'AND NOT EXISTS(SELECT 1 FROM mix_choice_genre_members m WHERE m.set_id=d.id)'),
        ('mix_choice_raw_genres', 'id', 'NOT EXISTS(SELECT 1 FROM mix_choice_genre_members m WHERE m.genre_id=d.id)'))
    for table, key, predicate in predicates:
        columns = key.strip('()')
        count = conn.execute(f'DELETE FROM {table} WHERE {key} IN (SELECT {columns} FROM {table} d WHERE {predicate} LIMIT ?)',
                             (limit,)).rowcount
        if count:
            return count
    return 0
