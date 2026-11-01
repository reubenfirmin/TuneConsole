"""Disposable musical profiles and durable, bounded source dependency work."""
from contextlib import ExitStack
import json
import hashlib

from yt_playlist.core import metadata_native, metadata_evidence, metadata_runtime
from yt_playlist.core.preparation import metadata_destinations
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


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
    def source_dependencies(self, key):
        """Bound a source publication to its current immutable metadata references."""
        dependencies = {(table, 'key:' + key) for table in (
            'rec_musical_pending', 'rec_musical_sources', 'rec_musical_links', 'rec_musical_source_profiles')}
        # A format cutover changes which immutable source references are read.
        dependencies.add(('metadata_runtime_state', 'group:format'))
        if metadata_runtime.active(self.conn):
            row = self.conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
            return dependencies, ('runtime', row[0] if row else None)
        if not metadata_native.available(self.conn):
            dependencies.update((table, 'key:1') for table in (
                metadata_destinations.publication.table('head'), metadata_destinations.table('head')))
            identity = metadata_destinations.selected(self.conn)
            references = () if identity is None else metadata_destinations.source_references(self.conn,key,identity)
            if identity is not None:
                dependencies.add((metadata_destinations.table('native'), 'key:' + key))
                dependencies.update((metadata_destinations.table('deliveries'), 'key:' + str(row[1]))
                                    for row in references)
            return dependencies, (identity, references)
        dependencies.add(('musical_source_observations', 'key:' + key))
        references = self.conn.execute(
            'SELECT o.id,o.delivery_id FROM musical_source_observations s '
            'JOIN native_metadata_observations o ON o.id=s.observation_id '
            'WHERE s.source_key=? ORDER BY s.field', (key,)).fetchall()
        dependencies.update(('native_metadata_observations', 'key:' + str(row[0])) for row in references)
        dependencies.update(('metadata_deliveries', 'key:' + str(row[1])) for row in references)
        return dependencies, tuple(row[0] for row in references)

    @synchronized
    def source_input(self, key, limit=32, max_bytes=256 * 1024, *, observation_ids=None):
        with ExitStack() as guards:
            typed = metadata_native.available(self.conn)
            tables = ('rec_musical_pending', 'rec_musical_sources', 'rec_musical_links', 'rec_musical_source_profiles')
            if typed:
                tables += ('musical_source_observations',)
            for table in tables:
                guards.enter_context(keyed_reads(self._store, table, [key]))
            pending = self.conn.execute('SELECT revision,cursor FROM rec_musical_pending WHERE source_key=?',
                                        (key,)).fetchone()
            if not pending:
                return None
            if typed and observation_ids is not None:
                if metadata_runtime.active(self.conn):
                    current = ('runtime', self.conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?',
                                                            (key,)).fetchone()[0])
                else:
                    current = tuple(row[0] for row in self.conn.execute(
                        'SELECT observation_id FROM musical_source_observations WHERE source_key=? ORDER BY field', (key,)))
                # References may change between dependency selection and reader
                # pinning. Retry with their new scope rather than reading outside it.
                if current != observation_ids:
                    return None
            identity = None if typed else metadata_destinations.selected(self.conn,store=self._store)
            prepared = identity is not None
            if not typed and observation_ids is not None:
                # Selection can change after dependency selection, including
                # the first selection of a previously unprepared source.
                if identity != observation_ids[0]:
                    return None
                references = (metadata_destinations.source_references(self.conn,key,identity,store=self._store)
                              if prepared else ())
                if references != observation_ids[1]:
                    return None
            if not typed and not prepared:
                size = self.conn.execute('SELECT length(CAST(payload AS BLOB)) FROM rec_musical_sources WHERE source_key=?',
                                         (key,)).fetchone()[0]
                if size > max_bytes:
                    raise WorkBudgetExceeded('Source profile input exceeds byte budget')
            source = self.conn.execute('SELECT '+('provider,scope,entity_id' if prepared else '*')+
                ' FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
            if typed:
                observations = metadata_native.source_values(self.conn, key, max_bytes, store=self._store)
            elif prepared:
                observations = metadata_destinations.source_values(self.conn,key,max_bytes,identity=identity,store=self._store)
            else:
                observations = json.loads(source['payload'])
            # Guard another publisher of the same source, including a missing cache row.
            self.conn.execute('SELECT digest FROM rec_musical_source_profiles WHERE source_key=?', (key,)).fetchone()
            ids = [r[0] for r in self.conn.execute('SELECT recording_id FROM rec_musical_links '
                'WHERE source_key=? AND recording_id>? ORDER BY recording_id LIMIT ?', (key, pending['cursor'], limit))]
            return ({'provider': source['provider'], 'scope': source['scope'], 'entity_id': source['entity_id'],
                     'observations': observations}, tuple(pending), ids)

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
                typed = metadata_evidence.available(self.conn)
                with ExitStack() as choice_guards:
                    for table in (('metadata_runtime_choices',) if metadata_runtime.active(self.conn) else
                                  ('metadata_choice_heads', 'metadata_observations') if typed else ('entry_field_choices',)):
                        choice_guards.enter_context(keyed_reads(self._store, table, entries))
                    for entry in entries:
                        if typed:
                            for row in self.conn.execute('SELECT o.* FROM metadata_choice_heads h '
                                    'JOIN metadata_observations o ON o.id=h.observation_id '
                                    "WHERE h.entry_id=? AND o.value_kind<>'withdrawn'", (entry,)):
                                value = metadata_evidence.value_from_row(row).selected()
                                choices.setdefault(row['field'], set()).add(json.dumps(value))
                        else:
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

    @snapshot_read
    def by_keys(self, keys):
        if len(keys) > 512:
            raise ValueError('Musical profile page exceeds 512 identities')
        with keyed_reads(self._store, 'rec_musical_profiles', keys):
            qs = ','.join('?' for _ in keys)
            result = {r[0]: json.loads(r[1]) for r in self.conn.execute(
                f'SELECT identity_key,payload FROM rec_musical_profiles WHERE identity_key IN ({qs})', keys)}
        return self._store.vibe_predictions.overlay(result)

    @snapshot_read
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
