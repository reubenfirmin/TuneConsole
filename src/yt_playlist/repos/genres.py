"""Canonical genre identities, sourced labels, and the user's enabled vocabulary."""
import json
import time

from yt_playlist.repos.base import Repo, lookup_read, synchronized
from yt_playlist.core import genre_identity, recommendation_invalidation as invalidation, taste_axes
from yt_playlist.util import genre_catalog


class GenreRepo(Repo):
    @synchronized
    def taxonomy(self):
        """Current identities, labels and family assignments for genre controls."""
        families = dict(self.conn.execute('SELECT key,name FROM genre_family'))
        redirects = (dict(self.conn.execute('SELECT retired_key,genre_key FROM genre_redirects'))
                     if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone()
                     else {})
        aliases = {}
        for key,label,source in self.conn.execute('SELECT genre_key,label,source FROM genre_alias'):
            if source != 'spelling':
                aliases.setdefault(redirects.get(key,key),set()).add(label)
        genres = []
        for key,name,family in self.conn.execute('SELECT key,name,family_key FROM genre_catalog ORDER BY key'):
            if key not in redirects:
                genres.append({'key':key,'name':name,'family':family or 'other:'+key,
                               'aliases':sorted(aliases.get(key,set())-{name},key=str.casefold)})
        return {'families':families,'genres':genres}

    @lookup_read
    def feature_axes(self, labels):
        """Detach current genre identity/family meanings for one model calculation."""
        return genre_identity.feature_axes(self.conn, labels)

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
    def rename_genre(self, key, name):
        """Rename a label while keeping all canonical references and old aliases."""
        if not isinstance(name,str) or not name.strip() or len(name.encode()) > 1024:
            raise ValueError('A genre requires 1..1024 bytes of nonblank text')
        with self.conn:
            key = genre_identity.current_key(self.conn,key)
            old = self.conn.execute('SELECT name FROM genre_catalog WHERE key=?',(key,)).fetchone()
            if old is None:
                raise ValueError('Unknown genre identity')
            resolved, reason = genre_identity.resolve(self.conn,name)
            if reason == 'ambiguous_genre' or resolved not in (None,key):
                raise ValueError('Genre name conflicts with another identity')
            self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom') "
                              'ON CONFLICT(label_key) DO NOTHING',
                              (genre_catalog.normalize(old[0]),old[0],key))
            self.conn.execute('UPDATE genre_catalog SET name=? WHERE key=?',(name,key))
            invalidation.catalog_changed(self.conn)

    @synchronized
    def set_genre_alias(self, label, key):
        if not isinstance(label,str) or not genre_catalog.normalize(label):
            raise ValueError('An alias requires a nonblank label')
        with self.conn:
            key = genre_identity.current_key(self.conn,key)
            if not self.conn.execute('SELECT 1 FROM genre_catalog WHERE key=?',(key,)).fetchone():
                raise ValueError('Unknown genre identity')
            # An alias cannot override another identity's actual name or key.
            conflicting = self.conn.execute('SELECT key FROM genre_catalog WHERE (key=? OR name=? COLLATE NOCASE) '
                                            'AND key<>?',(label,label,key)).fetchall()
            if any(genre_identity.current_key(self.conn,r[0]) != key for r in conflicting):
                raise ValueError('Alias conflicts with a genre identity')
            self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom') "
                              "ON CONFLICT(label_key) DO UPDATE SET label=excluded.label,genre_key=excluded.genre_key,source='custom'",
                              (genre_catalog.normalize(label),label,key))
            invalidation.catalog_changed(self.conn)

    @synchronized
    def set_genre_family(self, key, family_key):
        with self.conn:
            key = genre_identity.current_key(self.conn,key)
            if not self.conn.execute('SELECT 1 FROM genre_catalog WHERE key=?',(key,)).fetchone():
                raise ValueError('Unknown genre identity')
            self.conn.execute("UPDATE genre_catalog SET family_key=?,family_source='custom' WHERE key=?",(family_key,key))
            invalidation.catalog_changed(self.conn)

    @synchronized
    def merge_genres(self, retired_key, key, *, now=None):
        """Reassign current selections; immutable observations/predictions retain lineage.

        Conflicting current numeric axes need an explicit user decision. A merge
        never silently discards one learned value. Explicit disabled membership
        wins when the two preference records differ; both originals are archived.
        """
        with self.conn:
            self.conn.execute('UPDATE genre_catalog SET name=name WHERE 0')
            source, target = genre_identity.current_key(self.conn,retired_key), genre_identity.current_key(self.conn,key)
            if source == target:
                return
            if self.conn.execute('SELECT count(*) FROM genre_catalog WHERE key IN (?,?)',(source,target)).fetchone()[0] != 2:
                raise ValueError('Unknown genre identity')
            axis_changes = []
            if taste_axes.available(self.conn):
                old_axis = self.conn.execute("SELECT id FROM taste_axes WHERE kind='genre' AND genre_key=?",(source,)).fetchone()
                if old_axis:
                    new_axis = taste_axes.identity(self.conn,'genre:'+target,create=True)
                    for table in ('rec_weights','rec_lean','rec_theme','play_gradient_checkpoints','home_hidden_facet'):
                        if table != 'home_hidden_facet' and self.conn.execute(
                                f'SELECT count(*) FROM {table} WHERE axis_id IN (?,?)',(old_axis[0],new_axis)).fetchone()[0] == 2:
                            raise ValueError('Conflicting current taste axes in '+table)
                        axis_changes.append((table,old_axis[0],new_axis))
            prefs = list(self.conn.execute('SELECT genre_key,enabled,origin FROM genre_preferences WHERE genre_key IN (?,?)',
                                           (source,target)))
            for row in prefs:
                self.conn.execute('INSERT INTO genre_merge_preferences(retired_key,genre_key,enabled,origin,merged_at) '
                                  'VALUES (?,?,?,?,?)',(row[0],target,row[1],row[2],time.time() if now is None else now))
            self.conn.execute('DELETE FROM genre_preferences WHERE genre_key IN (?,?)',(source,target))
            if prefs:
                explicit = [r for r in prefs if r['origin'] in ('user','custom','legacy')]
                considered = explicit or prefs
                enabled = min(row['enabled'] for row in considered)
                origin = 'user' if explicit else 'default'
                self.conn.execute('INSERT INTO genre_preferences VALUES (?,?,?)',(target,enabled,origin))
            for table in ('recordings','albums','discovered_artists'):
                self.conn.execute(f'UPDATE {table} SET genre_key=? WHERE genre_key=?',(target,source))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recipe_genres'").fetchone():
                self.conn.execute('UPDATE recipe_genres SET genre_key=? WHERE genre_key=?',(target,source))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='home_genre_request_items'").fetchone():
                self.conn.execute('UPDATE home_genre_request_items SET genre_key=? WHERE genre_key=?',(target,source))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='album_menu_control_genres'").fetchone():
                self.conn.execute('UPDATE album_menu_control_genres SET genre_key=? WHERE genre_key=?',(target,source))
            for table, old_axis, new_axis in axis_changes:
                if table == 'home_hidden_facet':
                    self.conn.execute('INSERT OR IGNORE INTO home_hidden_facet(axis_id) SELECT ? FROM home_hidden_facet '
                                      'WHERE axis_id=?',(new_axis,old_axis))
                    self.conn.execute('DELETE FROM home_hidden_facet WHERE axis_id=?',(old_axis,))
                else:
                    self.conn.execute(f'UPDATE {table} SET axis_id=? WHERE axis_id=?',(new_axis,old_axis))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='playlist_recipe_facets'").fetchone():
                for old_axis,new_axis in {(old,new) for _,old,new in axis_changes}:
                    self.conn.execute('UPDATE playlist_recipe_facets SET axis_id=? WHERE axis_id=?',(new_axis,old_axis))
            for table in ('genre_alias','genre_external_id'):
                self.conn.execute(f'UPDATE {table} SET genre_key=? WHERE genre_key=?',(target,source))
            from yt_playlist.core import discovery_provenance
            if discovery_provenance.available(self.conn):
                discovery_provenance.merge_origins(self.conn,'genre_key',source,target,reason='explicit genre merge')
            self.conn.execute('UPDATE genre_redirects SET genre_key=? WHERE genre_key=?',(target,source))
            self.conn.execute('INSERT INTO genre_redirects VALUES (?,?)',(source,target))
            invalidation.catalog_changed(self.conn)

    @synchronized
    def enabled_matcher(self):
        """Provider aliases use current labels and exclude ambiguous spellings."""
        from yt_playlist.core.preparation import genre_preference_publication
        preferences = genre_preference_publication.relation(self.conn)
        if preferences is None:
            return None
        redirects = (dict(self.conn.execute('SELECT retired_key,genre_key FROM genre_redirects'))
                     if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone() else {})
        enabled = dict(self.conn.execute('SELECT c.key,c.name FROM '+preferences+' p '
                                        'JOIN genre_catalog c ON c.key=p.genre_key WHERE p.enabled=1'))
        matches = {}
        for label, key in self.conn.execute('SELECT name,key FROM genre_catalog UNION ALL '
                'SELECT key,key FROM genre_catalog UNION ALL SELECT label,genre_key FROM genre_alias'):
            matches.setdefault(genre_catalog.normalize(label),set()).add(redirects.get(key,key))
        return {label:enabled[next(iter(keys))] for label,keys in matches.items()
                if len(keys)==1 and next(iter(keys)) in enabled}

    @property
    def _keyed_preferences(self):
        return self.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                                 "AND name='genre_preferences'").fetchone() is not None

    @synchronized
    def configure_catalog(self, *, reset=False):
        """Seed/upgrade in one transaction, preserving additions and disabled old defaults.

        Enabled membership uses stable keys. Missing and explicitly disabled
        defaults remain distinct across catalog revisions.
        """
        def setting(key):
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

        records, labels = genre_catalog.catalog()
        defaults = genre_catalog.builtin_names()
        current = self.get_genre_whitelist()
        seeded = setting("genres_seeded")
        revision = setting("genre_catalog_revision")
        upgrading = revision != genre_catalog.REVISION
        if reset or not (seeded or current):
            target = defaults
        elif upgrading and current:
            previous = setting("genre_defaults")
            baseline = json.loads(previous) if previous is not None else genre_catalog.legacy_names()
            previous_keys = {genre_catalog.normalize(genre_catalog.canonical_name(n)) for n in baseline}
            active_keys = {genre_catalog.normalize(genre_catalog.canonical_name(n)) for n in current}
            blocked = set()
            if previous is None:
                # Splitting a mistaken old alias must not bypass a user's old removal.
                for name, aliases in genre_catalog.legacy_entries():
                    if genre_catalog.normalize(genre_catalog.canonical_name(name)) not in active_keys:
                        blocked.update(genre_catalog.normalize(genre_catalog.canonical_name(a))
                                       for a in [name, *aliases])
            target = [*current, *(n for n in defaults
                                  if genre_catalog.normalize(n) not in previous_keys | blocked)]
        else:
            target = current
        canonical = {}
        for name in target:
            name = genre_catalog.canonical_name(name)
            canonical.setdefault(genre_catalog.normalize(name), name)
        target = sorted(canonical.values(), key=str.casefold)

        with self.conn:
            if upgrading:
                self.conn.executemany(
                    "INSERT INTO genre_family(key,name) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                    list(genre_catalog.family_labels().items()))
                self.conn.executemany(
                    "INSERT INTO genre_catalog(key,name,family_key,source,family_source,description) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "family_key=CASE WHEN genre_catalog.family_source='custom' THEN genre_catalog.family_key "
                    "ELSE COALESCE(genre_catalog.family_key,excluded.family_key) END,source=excluded.source,"
                    "family_source=CASE WHEN genre_catalog.family_source='custom' THEN 'custom' "
                    "ELSE excluded.family_source END,description=excluded.description",
                    [(r["key"], r["name"], r["family"], r["source"], r["family_source"],
                      r.get("description", "")) for r in records.values()])
                self.conn.execute("DELETE FROM genre_alias WHERE source != 'custom'")
                self.conn.executemany(
                    "INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,?) "
                    "ON CONFLICT(label_key) DO NOTHING",
                    [(key, label, genre_identity.current_key(self.conn,genre), source)
                     for key, (genre, label, source) in labels.items()])
                self.conn.execute("DELETE FROM genre_external_id WHERE provider='musicbrainz'")
                self.conn.executemany(
                    "INSERT INTO genre_external_id(provider,external_id,genre_key) VALUES ('musicbrainz',?,?)",
                    [(r["mbid"], genre_identity.current_key(self.conn,r["key"])) for r in records.values() if r["mbid"]])
            for key, name in canonical.items():
                if key not in records and not self._keyed_preferences:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                        (key, name))
                    self.conn.execute(
                        "INSERT OR IGNORE INTO genre_alias(label_key,label,genre_key,source) "
                        "VALUES (?,?,?,'custom')", (key, name, key))
            if self._keyed_preferences:
                keys = {genre_identity.add_custom(self.conn, name) for name in target}
                # Record disabled introduced defaults as well as enabled ones.
                # Re-running an unchanged catalog cannot resurrect removals.
                self.conn.execute('UPDATE genre_preferences SET enabled=0')
                self.conn.executemany("INSERT OR IGNORE INTO genre_preferences VALUES (?,0,'default')",
                                      [(key,) for key in records if genre_identity.current_key(self.conn,key)==key])
                self.conn.executemany("INSERT INTO genre_preferences VALUES (?,1,?) "
                    "ON CONFLICT(genre_key) DO UPDATE SET enabled=1",
                    [(key, 'default' if key in records else 'custom') for key in keys])
            elif target != current:
                self.conn.execute("DELETE FROM genre_whitelist")
                self.conn.executemany("INSERT INTO genre_whitelist(name) VALUES (?)", [(n,) for n in target])
            self.conn.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", [
                ("genres_seeded", "1"), ("genre_catalog_revision", genre_catalog.REVISION),
                ("genre_defaults", json.dumps(defaults)),
            ])

    @synchronized
    def get_genre_whitelist(self) -> list:
        from yt_playlist.core.preparation import genre_preference_publication
        preferences = genre_preference_publication.relation(self.conn)
        query = ('SELECT c.name FROM '+preferences+' p JOIN genre_catalog c ON c.key=p.genre_key '
                 'WHERE p.enabled=1' if preferences is not None else 'SELECT name FROM genre_whitelist')
        rows = self.conn.execute(query).fetchall()
        return sorted((r["name"] for r in rows), key=str.lower)

    @synchronized
    def set_genres(self, names) -> None:
        with self.conn:
            if self._keyed_preferences:
                keys = {genre_identity.add_custom(self.conn, name) for name in names}
                self.conn.execute("UPDATE genre_preferences SET enabled=0,origin='user'")
                self.conn.executemany("INSERT INTO genre_preferences VALUES (?,1,'user') "
                    "ON CONFLICT(genre_key) DO UPDATE SET enabled=1,origin='user'", [(key,) for key in keys])
            else:
                self.conn.execute("DELETE FROM genre_whitelist")
                self.conn.executemany("INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)",
                                      [(n,) for n in names])

    @synchronized
    def add_genre(self, name) -> None:
        with self.conn:
            if self._keyed_preferences:
                key = genre_identity.add_custom(self.conn, name)
                self.conn.execute("INSERT INTO genre_preferences VALUES (?,1,'user') "
                    "ON CONFLICT(genre_key) DO UPDATE SET enabled=1,origin='user'", (key,))
            else:
                self.conn.execute("INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)", (name,))

    @synchronized
    def remove_genre(self, name) -> None:
        with self.conn:
            if self._keyed_preferences:
                key, reason = genre_identity.resolve(self.conn, name)
                if reason == 'ambiguous_genre':
                    raise ValueError('Ambiguous genre label: ' + name)
                if key is not None:
                    self.conn.execute("INSERT INTO genre_preferences VALUES (?,0,'user') "
                        "ON CONFLICT(genre_key) DO UPDATE SET enabled=0,origin='user'", (key,))
            else:
                self.conn.execute("DELETE FROM genre_whitelist WHERE name=?", (name,))

    @synchronized
    def all_genres(self) -> list:
        """Every distinct non-blank genre we've collected, case-insensitively alpha-sorted."""
        rows = self.conn.execute(
            "SELECT DISTINCT genre FROM tracks WHERE genre IS NOT NULL AND genre <> ''").fetchall()
        return sorted((r["genre"] for r in rows), key=str.lower)
