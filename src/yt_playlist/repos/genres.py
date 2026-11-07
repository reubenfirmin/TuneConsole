"""Canonical genre identities, sourced labels, and the user's enabled vocabulary."""
from contextlib import contextmanager

from yt_playlist.repos.base import Repo, lookup_read, synchronized
from yt_playlist.core import genre_identity, recommendation_invalidation as invalidation, taste_axes


class GenreRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic(join=True):
                yield

    @synchronized
    def taxonomy_facts(self):
        families = dict(self.conn.execute('SELECT key,name FROM genre_family'))
        redirects = (dict(self.conn.execute('SELECT retired_key,genre_key FROM genre_redirects'))
                     if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone()
                     else {})
        return {'families': families, 'redirects': redirects,
                'aliases': [tuple(row) for row in self.conn.execute('SELECT genre_key,label,source FROM genre_alias')],
                'genres': [tuple(row) for row in self.conn.execute(
                    'SELECT key,name,family_key FROM genre_catalog ORDER BY key')]}

    @lookup_read
    def axis_matches(self, labels):
        return genre_identity.axis_matches(self.conn, labels)

    @lookup_read
    def family_members(self, families):
        """Current, unretired genre identities in display-name order per family."""
        families = list(dict.fromkeys(families))
        result = {family: [] for family in families}
        redirected = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone()
        retired = ('AND NOT EXISTS(SELECT 1 FROM genre_redirects r WHERE r.retired_key=c.key) '
                   if redirected else '')
        for offset in range(0, len(families), 200):
            page = families[offset:offset + 200]
            for family, key in self.conn.execute(
                    'SELECT c.family_key,c.key FROM genre_catalog c WHERE c.family_key IN ('
                    + ','.join('?' for _ in page) + ') ' + retired + 'ORDER BY c.name COLLATE NOCASE', page):
                result[family].append(key)
        return result

    @synchronized
    def current_key(self, key):
        return genre_identity.current_key(self.conn, key)

    @synchronized
    def genre_name(self, key):
        row = self.conn.execute('SELECT name FROM genre_catalog WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    @synchronized
    def resolve(self, name):
        return genre_identity.resolve(self.conn, name)

    @synchronized
    def insert_custom(self, key, name):
        self.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", (key, name))
        self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom')",
                          (key, name, key))

    @synchronized
    def alias_conflicts(self, label, key):
        rows = self.conn.execute('SELECT key FROM genre_catalog WHERE (key=? OR name=? COLLATE NOCASE) '
                                 'AND key<>?', (label, label, key))
        return any(genre_identity.current_key(self.conn, row[0]) != key for row in rows)

    @synchronized
    def rename(self, key, name, *, old_label_key, old_label):
        self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom') "
                          'ON CONFLICT(label_key) DO NOTHING', (old_label_key, old_label, key))
        self.conn.execute('UPDATE genre_catalog SET name=? WHERE key=?', (name, key))

    @synchronized
    def save_alias(self, label_key, label, key):
        self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom') "
            "ON CONFLICT(label_key) DO UPDATE SET label=excluded.label,genre_key=excluded.genre_key,source='custom'",
            (label_key, label, key))

    @synchronized
    def save_family(self, key, family_key):
        self.conn.execute("UPDATE genre_catalog SET family_key=?,family_source='custom' WHERE key=?", (family_key, key))

    @synchronized
    def invalidate(self):
        invalidation.catalog_changed(self.conn)

    @synchronized
    def genre_axis(self, key):
        if not taste_axes.available(self.conn):
            return None
        row = self.conn.execute("SELECT id FROM taste_axes WHERE kind='genre' AND genre_key=?", (key,)).fetchone()
        return row[0] if row else None

    @synchronized
    def family_exists(self, key):
        return self.conn.execute('SELECT 1 FROM genre_family WHERE key=?', (key,)).fetchone() is not None

    @synchronized
    def axis_id(self, kind, key):
        column = {'genre': 'genre_key', 'family': 'family_key'}[kind]
        row = self.conn.execute(f'SELECT id FROM taste_axes WHERE kind=? AND {column}=?', (kind, key)).fetchone()
        return row[0] if row else None

    @synchronized
    def create_axis(self, kind, key):
        column = {'genre': 'genre_key', 'family': 'family_key'}[kind]
        return self.conn.execute(f'INSERT INTO taste_axes(kind,{column}) VALUES (?,?)', (kind, key)).lastrowid

    @synchronized
    def axis_counts(self, old_axis, new_axis):
        return {table: self.conn.execute(f'SELECT count(*) FROM {table} WHERE axis_id IN (?,?)',
                                         (old_axis, new_axis)).fetchone()[0]
                for table in ('rec_weights', 'rec_lean', 'rec_theme', 'play_gradient_checkpoints', 'home_hidden_facet')}

    @synchronized
    def preferences(self, source, target):
        return [dict(row) for row in self.conn.execute(
            'SELECT genre_key,enabled,origin FROM genre_preferences WHERE genre_key IN (?,?)', (source, target))]

    @synchronized
    def apply_merge(self, source, target, preference, *, old_axis, new_axis, now):
        prefs = self.preferences(source, target)
        for row in prefs:
            self.conn.execute('INSERT INTO genre_merge_preferences(retired_key,genre_key,enabled,origin,merged_at) '
                              'VALUES (?,?,?,?,?)', (row['genre_key'], target, row['enabled'], row['origin'], now))
        self.conn.execute('DELETE FROM genre_preferences WHERE genre_key IN (?,?)', (source, target))
        if preference is not None:
            self.conn.execute('INSERT INTO genre_preferences VALUES (?,?,?)', (target, *preference))
        for table in ('recordings', 'albums', 'discovered_artists'):
            self.conn.execute(f'UPDATE {table} SET genre_key=? WHERE genre_key=?', (target, source))
        for table in ('recipe_genres', 'home_genre_request_items', 'album_menu_control_genres'):
            if self.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone():
                self.conn.execute(f'UPDATE {table} SET genre_key=? WHERE genre_key=?', (target, source))
        if old_axis is not None:
            for table in ('rec_weights', 'rec_lean', 'rec_theme', 'play_gradient_checkpoints'):
                self.conn.execute(f'UPDATE {table} SET axis_id=? WHERE axis_id=?', (new_axis, old_axis))
            self.conn.execute('INSERT OR IGNORE INTO home_hidden_facet(axis_id) SELECT ? FROM home_hidden_facet '
                              'WHERE axis_id=?', (new_axis, old_axis))
            self.conn.execute('DELETE FROM home_hidden_facet WHERE axis_id=?', (old_axis,))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='playlist_recipe_facets'").fetchone():
                self.conn.execute('UPDATE playlist_recipe_facets SET axis_id=? WHERE axis_id=?', (new_axis, old_axis))
        for table in ('genre_alias', 'genre_external_id'):
            self.conn.execute(f'UPDATE {table} SET genre_key=? WHERE genre_key=?', (target, source))
        self.conn.execute('UPDATE genre_redirects SET genre_key=? WHERE genre_key=?', (target, source))
        self.conn.execute('INSERT INTO genre_redirects VALUES (?,?)', (source, target))

    @synchronized
    def matcher_facts(self):
        from yt_playlist.core.preparation import genre_preference_publication
        preferences = genre_preference_publication.relation(self.conn)
        if preferences is None:
            return None
        redirects = (dict(self.conn.execute('SELECT retired_key,genre_key FROM genre_redirects'))
                     if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone() else {})
        enabled = dict(self.conn.execute('SELECT c.key,c.name FROM '+preferences+' p '
                                        'JOIN genre_catalog c ON c.key=p.genre_key WHERE p.enabled=1'))
        return {'redirects': redirects, 'enabled': enabled, 'labels': [tuple(row) for row in self.conn.execute(
            'SELECT name,key FROM genre_catalog UNION ALL SELECT key,key FROM genre_catalog '
            'UNION ALL SELECT label,genre_key FROM genre_alias')]}

    @lookup_read
    def uses_keyed_preferences(self):
        return self.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                                 "AND name='genre_preferences'").fetchone() is not None

    @synchronized
    def configuration_settings(self):
        return dict(self.conn.execute("SELECT key,value FROM settings WHERE key IN "
            "('genres_seeded','genre_catalog_revision','genre_defaults')"))

    @synchronized
    def seed_catalog(self, records, labels, families):
        self.conn.executemany(
            "INSERT INTO genre_family(key,name) VALUES (?,?) ON CONFLICT(key) DO NOTHING", list(families.items()))
        self.conn.executemany(
            "INSERT INTO genre_catalog(key,name,family_key,source,family_source,description) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "family_key=CASE WHEN genre_catalog.family_source='custom' THEN genre_catalog.family_key "
            "ELSE COALESCE(genre_catalog.family_key,excluded.family_key) END,source=excluded.source,"
            "family_source=CASE WHEN genre_catalog.family_source='custom' THEN 'custom' "
            "ELSE excluded.family_source END,description=excluded.description",
            [(r['key'], r['name'], r['family'], r['source'], r['family_source'], r.get('description', ''))
             for r in records.values()])
        self.conn.execute("DELETE FROM genre_alias WHERE source != 'custom'")
        self.conn.executemany(
            "INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,?) ON CONFLICT(label_key) DO NOTHING",
            [(key, label, genre_identity.current_key(self.conn, genre), source)
             for key, (genre, label, source) in labels.items()])
        self.conn.execute("DELETE FROM genre_external_id WHERE provider='musicbrainz'")
        self.conn.executemany(
            "INSERT INTO genre_external_id(provider,external_id,genre_key) VALUES ('musicbrainz',?,?)",
            [(r['mbid'], genre_identity.current_key(self.conn, r['key'])) for r in records.values() if r['mbid']])

    @synchronized
    def insert_legacy_custom(self, key, name):
        self.conn.execute("INSERT OR IGNORE INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", (key, name))
        self.conn.execute("INSERT OR IGNORE INTO genre_alias(label_key,label,genre_key,source) "
                          "VALUES (?,?,?,'custom')", (key, name, key))

    @synchronized
    def configure_preferences(self, enabled, default_keys):
        self.conn.execute('UPDATE genre_preferences SET enabled=0')
        self.conn.executemany("INSERT OR IGNORE INTO genre_preferences VALUES (?,0,'default')",
                              [(key,) for key in default_keys if genre_identity.current_key(self.conn, key) == key])
        self.conn.executemany('INSERT INTO genre_preferences VALUES (?,1,?) '
                              'ON CONFLICT(genre_key) DO UPDATE SET enabled=1', enabled)

    @synchronized
    def save_configuration(self, values):
        self.conn.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', values.items())

    @synchronized
    def get_genre_whitelist(self) -> list:
        from yt_playlist.core.preparation import genre_preference_publication
        preferences = genre_preference_publication.relation(self.conn)
        query = ('SELECT c.name FROM '+preferences+' p JOIN genre_catalog c ON c.key=p.genre_key '
                 'WHERE p.enabled=1' if preferences is not None else 'SELECT name FROM genre_whitelist')
        rows = self.conn.execute(query).fetchall()
        return sorted((r["name"] for r in rows), key=str.lower)

    @synchronized
    def replace_preferences(self, keys, *, origin):
        self.conn.execute('UPDATE genre_preferences SET enabled=0,origin=?', (origin,))
        self.conn.executemany('INSERT INTO genre_preferences VALUES (?,1,?) '
            'ON CONFLICT(genre_key) DO UPDATE SET enabled=1,origin=excluded.origin', [(key, origin) for key in keys])

    @synchronized
    def save_preference(self, key, *, enabled, origin):
        self.conn.execute('INSERT INTO genre_preferences VALUES (?,?,?) '
            'ON CONFLICT(genre_key) DO UPDATE SET enabled=excluded.enabled,origin=excluded.origin', (key, int(enabled), origin))

    @synchronized
    def replace_legacy_whitelist(self, names):
        self.conn.execute('DELETE FROM genre_whitelist')
        self.conn.executemany('INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)', [(name,) for name in names])

    @synchronized
    def add_legacy_genre(self, name):
        self.conn.execute('INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)', (name,))

    @synchronized
    def remove_legacy_genre(self, name):
        self.conn.execute('DELETE FROM genre_whitelist WHERE name=?', (name,))

    @synchronized
    def all_genres(self) -> list:
        """Every distinct non-blank genre we've collected, case-insensitively alpha-sorted."""
        rows = self.conn.execute(
            "SELECT DISTINCT genre FROM tracks WHERE genre IS NOT NULL AND genre <> ''").fetchall()
        return sorted((r["genre"] for r in rows), key=str.lower)
