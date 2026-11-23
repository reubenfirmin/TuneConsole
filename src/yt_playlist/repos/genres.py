"""Canonical genre identities, sourced labels, and the user's enabled vocabulary."""
from contextlib import contextmanager

from yt_playlist.core.catalog_query import LIBRARY_TRACKS_SQL
from yt_playlist.repos.base import Repo, lookup_read, synchronized
from yt_playlist.core import genre_identity


class GenreRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic(join=True):
                yield

    @synchronized
    def taxonomy_facts(self):
        families = dict(self.conn.execute('SELECT key,name FROM genre_family'))
        redirects = self._redirects()
        return {'families': families, 'redirects': redirects,
                'aliases': [tuple(row) for row in self.conn.execute('SELECT genre_key,label,source FROM genre_alias')],
                'genres': [tuple(row) for row in self.conn.execute(
                    'SELECT key,name,family_key FROM genre_catalog ORDER BY key')]}

    @lookup_read
    def axis_matches(self, labels):
        return genre_identity.axis_match_rows(self.conn, labels)

    @lookup_read
    def family_members(self, families):
        """Current, unretired genre identities in display-name order per family."""
        families = list(dict.fromkeys(families))
        result = {family: [] for family in families}
        for offset in range(0, len(families), 200):
            page = families[offset:offset + 200]
            for family, key in self.conn.execute(
                    'SELECT c.family_key,c.key FROM genre_catalog c WHERE c.family_key IN ('
                    + ','.join('?' for _ in page) + ') '
                    'AND c.redirect_key IS NULL '
                    'ORDER BY c.name COLLATE NOCASE', page):
                result[family].append(key)
        return result

    @synchronized
    def genre_name(self, key):
        row = self.conn.execute('SELECT name FROM genre_catalog WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    @lookup_read
    def label_matches(self, label, normalized):
        return genre_identity.matching_keys(self.conn, label, normalized)

    @synchronized
    def insert_custom(self, key, name):
        self.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", (key, name))
        self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom')",
                          (key, name, key))

    @synchronized
    def matcher_facts(self):
        redirects = self._redirects()
        enabled = dict(self.conn.execute(
            'SELECT key,name FROM genre_catalog WHERE enabled=1 AND redirect_key IS NULL'))
        return {'redirects': redirects, 'enabled': enabled, 'labels': [tuple(row) for row in self.conn.execute(
            'SELECT name,key FROM genre_catalog UNION ALL SELECT key,key FROM genre_catalog '
            'UNION ALL SELECT label,genre_key FROM genre_alias')]}

    @synchronized
    def configuration_settings(self):
        return dict(self.conn.execute("SELECT key,value FROM settings WHERE key IN "
            "('genres_seeded','genre_catalog_revision','genre_defaults')"))

    @synchronized
    def seed_catalog(self, records, labels, families):
        redirects = self._redirects()
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
            [(key, label, redirects.get(genre, genre), source)
             for key, (genre, label, source) in labels.items()])

    def _redirects(self):
        return dict(self.conn.execute('SELECT key,redirect_key FROM genre_catalog WHERE redirect_key IS NOT NULL'))

    @synchronized
    def configure_preferences(self, enabled, default_keys):
        self.conn.execute('UPDATE genre_catalog SET enabled=0 WHERE enabled=1')
        self.conn.executemany(
            "UPDATE genre_catalog SET preference_origin='default' "
            "WHERE key=? AND preference_origin IS NULL AND redirect_key IS NULL",
            [(key,) for key in default_keys])
        self.conn.executemany('UPDATE genre_catalog SET enabled=1,preference_origin=COALESCE(preference_origin,?) '
                              'WHERE key=? AND redirect_key IS NULL', [(origin, key) for key, origin in enabled])

    @synchronized
    def save_configuration(self, values):
        self.conn.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', values.items())

    @synchronized
    def get_genre_whitelist(self) -> list:
        rows = self.conn.execute('SELECT name FROM genre_catalog WHERE enabled=1 AND redirect_key IS NULL').fetchall()
        return sorted((r["name"] for r in rows), key=str.lower)

    @synchronized
    def replace_preferences(self, keys, *, origin):
        self.conn.execute('UPDATE genre_catalog SET enabled=0,preference_origin=? WHERE preference_origin IS NOT NULL',
                          (origin,))
        self.conn.executemany('UPDATE genre_catalog SET enabled=1,preference_origin=? WHERE key=?',
                              [(origin, key) for key in keys])

    @synchronized
    def save_preference(self, key, *, enabled, origin):
        self.conn.execute('UPDATE genre_catalog SET enabled=?,preference_origin=? WHERE key=?',
                          (int(enabled), origin, key))

    @synchronized
    def all_genres(self) -> list:
        """Every distinct non-blank genre we've collected, case-insensitively alpha-sorted."""
        rows = self.conn.execute(
            f"SELECT DISTINCT genre FROM ({LIBRARY_TRACKS_SQL}) WHERE genre IS NOT NULL AND genre <> ''").fetchall()
        return sorted((r["genre"] for r in rows), key=str.lower)
