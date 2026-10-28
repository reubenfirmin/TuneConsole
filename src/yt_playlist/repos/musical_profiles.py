"""Disposable musical profiles and durable, bounded source dependency work."""
from contextlib import ExitStack
import json
import hashlib

from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.repos.base import Repo, synchronized


class MusicalProfilesRepo(Repo):
    @synchronized
    def stale(self, version, limit=32):
        result = []
        # Two index ranges avoid scanning a current corpus on every idle tick.
        for table, key in (('rec_musical_source_profiles', 'source_key'), ('rec_musical_profiles', 'identity_key')):
            values = []
            for operator in ('<', '>'):
                values.extend(r[0] for r in self.conn.execute(
                    f'SELECT {key} FROM {table} WHERE version{operator}? LIMIT ?', (version, limit - len(values))))
            result.append(values)
        return tuple(result)

    @synchronized
    def queue_stale(self, version, limit=32):
        sources, keys = self.stale(version, limit)
        with self.conn:
            for key in sources:
                self.conn.execute('INSERT OR IGNORE INTO rec_musical_pending(source_key,revision) '
                    'SELECT source_key,revision FROM rec_musical_sources WHERE source_key=?', (key,))
            for key in keys:
                self.conn.execute('INSERT OR IGNORE INTO rec_content_pending(identity_key) VALUES (?)', (key,))

    @synchronized
    def pending(self):
        row = self.conn.execute('SELECT source_key FROM rec_musical_pending ORDER BY position LIMIT 1').fetchone()
        return row[0] if row else None

    @synchronized
    def source_input(self, key, limit=32, max_bytes=256 * 1024):
        with ExitStack() as guards:
            for table in ('rec_musical_pending', 'rec_musical_sources', 'rec_musical_links',
                          'rec_musical_source_profiles'):
                guards.enter_context(keyed_reads(self._store, table, [key]))
            pending = self.conn.execute('SELECT revision,cursor FROM rec_musical_pending WHERE source_key=?',
                                        (key,)).fetchone()
            if not pending:
                return None
            size = self.conn.execute('SELECT length(CAST(payload AS BLOB)) FROM rec_musical_sources WHERE source_key=?',
                                      (key,)).fetchone()[0]
            if size > max_bytes:
                raise WorkBudgetExceeded('Source profile input exceeds byte budget')
            source = self.conn.execute('SELECT * FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
            # Guard another publisher of the same source, including a missing cache row.
            self.conn.execute('SELECT digest FROM rec_musical_source_profiles WHERE source_key=?', (key,)).fetchone()
            ids = [r[0] for r in self.conn.execute('SELECT recording_id FROM rec_musical_links '
                'WHERE source_key=? AND recording_id>? ORDER BY recording_id LIMIT ?', (key, pending['cursor'], limit))]
            return ({'provider': source['provider'], 'scope': source['scope'], 'entity_id': source['entity_id'],
                     'observations': json.loads(source['payload'])}, tuple(pending), ids)

    @synchronized
    def publish_source(self, key, pending, ids, payload, version, digest, now, limit=32):
        self._guard()
        self.conn.execute('INSERT INTO rec_musical_source_profiles VALUES (?,?,?,?,?) '
            'ON CONFLICT(source_key) DO UPDATE SET payload=excluded.payload,version=excluded.version,'
            'digest=excluded.digest,prepared_at=excluded.prepared_at', (key, payload, version, digest, now))
        for rid in ids:
            self.conn.execute('INSERT INTO rec_content_pending(identity_key) '
                'SELECT identity_key FROM recordings WHERE id=? '
                'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1', (rid,))
        revision, cursor = pending
        self.conn.execute('DELETE FROM rec_musical_pending WHERE source_key=? AND revision=? AND cursor=?',
                          (key, revision, cursor))
        if len(ids) == limit:
            # Move a large artist to the back so a hot source cannot starve others.
            self.conn.execute('INSERT INTO rec_musical_pending(source_key,revision,cursor) VALUES (?,?,?)',
                              (key, revision, ids[-1]))

    @synchronized
    def rotate(self, key):
        with self.conn:
            row = self.conn.execute('SELECT revision,cursor FROM rec_musical_pending WHERE source_key=?', (key,)).fetchone()
            if row:
                self.conn.execute('DELETE FROM rec_musical_pending WHERE source_key=?', (key,))
                self.conn.execute('INSERT INTO rec_musical_pending(source_key,revision,cursor) VALUES (?,?,?)',
                                  (key, *row))

    @synchronized
    def recording_inputs(self, keys, max_bytes=8 * 1024 * 1024):
        """Bound each recording's source set; choices stay attached to catalog entries."""
        result = {}
        used_bytes = 0
        with ExitStack() as guards:
            guards.enter_context(keyed_reads(self._store, 'recordings', keys))
            guards.enter_context(keyed_reads(self._store, 'rec_musical_profiles', keys))
            for key in keys:
                recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
                self.conn.execute('SELECT digest FROM rec_musical_profiles WHERE identity_key=?', (key,)).fetchone()
                if not recording:
                    result[key] = ([], {}, False)
                    continue
                rid = recording[0]
                with keyed_reads(self._store, 'rec_musical_links', [rid]):
                    links = [r[0] for r in self.conn.execute('SELECT source_key FROM rec_musical_links '
                        'WHERE recording_id=? ORDER BY source_key LIMIT 65', (rid,))]
                profiles = []
                with keyed_reads(self._store, 'rec_musical_source_profiles', links[:64]):
                    for link in links[:64]:
                        size = self.conn.execute('SELECT length(CAST(payload AS BLOB)) '
                            'FROM rec_musical_source_profiles WHERE source_key=?', (link,)).fetchone()
                        used_bytes += size[0] if size else 0
                        if used_bytes > max_bytes:
                            raise WorkBudgetExceeded('Recording profile inputs exceed byte budget')
                        row = self.conn.execute('SELECT payload FROM rec_musical_source_profiles WHERE source_key=?',
                                                (link,)).fetchone()
                        if row:
                            profiles.append(json.loads(row[0]))
                with keyed_reads(self._store, 'catalog_entries', [rid]):
                    entries = [r[0] for r in self.conn.execute('SELECT id FROM catalog_entries WHERE recording_id=?', (rid,))]
                choices = {}
                with keyed_reads(self._store, 'entry_field_choices', entries):
                    for entry in entries:
                        for field, value in self.conn.execute('SELECT field,value_json FROM entry_field_choices WHERE entry_id=?',
                                                               (entry,)):
                            choices.setdefault(field, set()).add(value)
                # Conflicting explicit entry choices block inherited fallback.
                overrides = {field: json.loads(next(iter(values))) if len(values) == 1 else None
                             for field, values in choices.items()}
                result[key] = (profiles, overrides, len(links) > 64)
        return result

    @synchronized
    def publish_recordings(self, rows, now):
        self._guard()
        changed = False
        for key, payload, version, digest in rows:
            if payload is None:
                cursor = self.conn.execute('DELETE FROM rec_musical_profiles WHERE identity_key=?', (key,))
            else:
                profile = json.loads(payload)
                if 'vibes' in profile:
                    profile.pop('vibes')
                    payload = json.dumps(profile, sort_keys=True, separators=(',', ':'), allow_nan=False)
                    digest = hashlib.sha256(payload.encode()).hexdigest()
                cursor = self.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?) '
                    'ON CONFLICT(identity_key) DO UPDATE SET payload=excluded.payload,version=excluded.version,'
                    'digest=excluded.digest,prepared_at=excluded.prepared_at '
                    'WHERE digest<>excluded.digest OR version<>excluded.version',
                    (key, payload, version, digest, now))
            changed = changed or cursor.rowcount > 0
        if changed:
            self._store.rec_refresh.publish_notification()

    @synchronized
    def by_keys(self, keys):
        if len(keys) > 512:
            raise ValueError('Musical profile page exceeds 512 identities')
        with keyed_reads(self._store, 'rec_musical_profiles', keys):
            qs = ','.join('?' for _ in keys)
            result = {r[0]: json.loads(r[1]) for r in self.conn.execute(
                f'SELECT identity_key,payload FROM rec_musical_profiles WHERE identity_key IN ({qs})', keys)}
        return self._store.vibe_predictions.overlay(result)

    @synchronized
    def provenance_sources(self, key, digests, *, limit=16, max_bytes=256 * 1024):
        """Read bounded cached evidence actually used by a recording's saved profile.

        A newer source cache must not be presented as the cause of an older
        recording result. No preparation or provider lookup runs on this path.
        """
        if not digests:
            return [], False
        digests = list(dict.fromkeys(digests))[:64]
        qs = ','.join('?' for _ in digests)
        rows = self.conn.execute(
            'SELECT CASE WHEN length(CAST(p.payload AS BLOB))<=? THEN p.payload END,p.digest '
            'FROM recordings r JOIN rec_musical_links l ON l.recording_id=r.id '
            'JOIN rec_musical_source_profiles p ON p.source_key=l.source_key '
            f'WHERE r.identity_key=? AND p.digest IN ({qs}) ORDER BY p.source_key LIMIT ?',
            (max_bytes, key, *digests, limit + 1))
        sources, found, used = [], set(), 0
        for raw, digest in rows:
            if raw is None or len(sources) == limit:
                break
            used += len(raw.encode())
            if used > max_bytes:
                break
            sources.append(json.loads(raw))
            found.add(digest)
        return sources, set(digests) != found

    def _guard(self):
        if not self.conn.in_transaction:
            raise RuntimeError('Musical profiles require guarded publication')
