"""Canonical genre identities, sourced labels, and the user's enabled vocabulary."""
import json

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.util import genre_catalog


class GenreRepo(Repo):
    @synchronized
    def configure_catalog(self, *, reset=False):
        """Seed/upgrade in one transaction, preserving additions and disabled old defaults.

        Keep the old text whitelist as the enabled slice for existing callers and saved songs.
        The catalog retains disabled identities, so removing a primary name cannot resurrect
        that genre through one of its aliases. No saved track or taste key is rewritten.
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
                    "INSERT OR REPLACE INTO genre_family(key,name) VALUES (?,?)",
                    list(genre_catalog.family_labels().items()))
                self.conn.executemany(
                    "INSERT INTO genre_catalog(key,name,family_key,source,family_source,description) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "name=excluded.name,family_key=excluded.family_key,source=excluded.source,"
                    "family_source=excluded.family_source,description=excluded.description",
                    [(r["key"], r["name"], r["family"], r["source"], r["family_source"],
                      r.get("description", "")) for r in records.values()])
                self.conn.execute("DELETE FROM genre_alias WHERE source != 'custom'")
                self.conn.executemany(
                    "INSERT OR REPLACE INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,?)",
                    [(key, label, genre, source) for key, (genre, label, source) in labels.items()])
                self.conn.execute("DELETE FROM genre_external_id WHERE provider='musicbrainz'")
                self.conn.executemany(
                    "INSERT INTO genre_external_id(provider,external_id,genre_key) VALUES ('musicbrainz',?,?)",
                    [(r["mbid"], r["key"]) for r in records.values() if r["mbid"]])
            for key, name in canonical.items():
                if key not in records:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                        (key, name))
                    self.conn.execute(
                        "INSERT OR IGNORE INTO genre_alias(label_key,label,genre_key,source) "
                        "VALUES (?,?,?,'custom')", (key, name, key))
            if target != current:
                self.conn.execute("DELETE FROM genre_whitelist")
                self.conn.executemany("INSERT INTO genre_whitelist(name) VALUES (?)", [(n,) for n in target])
            self.conn.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", [
                ("genres_seeded", "1"), ("genre_catalog_revision", genre_catalog.REVISION),
                ("genre_defaults", json.dumps(defaults)),
            ])

    @synchronized
    def get_genre_whitelist(self) -> list:
        rows = self.conn.execute("SELECT name FROM genre_whitelist").fetchall()
        return sorted((r["name"] for r in rows), key=str.lower)

    @synchronized
    def set_genres(self, names) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM genre_whitelist")
            self.conn.executemany("INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)",
                                  [(n,) for n in names])

    @synchronized
    def add_genre(self, name) -> None:
        self.conn.execute("INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)", (name,))
        self.conn.commit()

    @synchronized
    def remove_genre(self, name) -> None:
        self.conn.execute("DELETE FROM genre_whitelist WHERE name=?", (name,))
        self.conn.commit()

    @synchronized
    def all_genres(self) -> list:
        """Every distinct non-blank genre we've collected, case-insensitively alpha-sorted."""
        rows = self.conn.execute(
            "SELECT DISTINCT genre FROM tracks WHERE genre IS NOT NULL AND genre <> ''").fetchall()
        return sorted((r["genre"] for r in rows), key=str.lower)
