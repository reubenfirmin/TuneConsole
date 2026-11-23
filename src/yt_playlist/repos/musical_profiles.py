"""Disposable musical profiles and durable, bounded source dependency work."""
from contextlib import ExitStack
from yt_playlist.core import runtime_metadata_choices
import json

from yt_playlist.core import content_work, metadata_choice_selections, metadata_native, metadata_evidence
from yt_playlist.core import metadata_runtime, musical_source_queue, profile_files
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.audio_vibe_contract import score_version_filter
from yt_playlist.core.metadata_contract import ACTIVE
from yt_playlist.core.metadata_file_contract import owner_key
from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.repos import profile_file_writes

MAX_MASKED_SOURCE_LINKS = 4096


class MusicalProfilesRepo(Repo):
    @synchronized
    def stale(self, version, limit=32):
        result = []
        # Two index ranges avoid scanning a current corpus on every idle tick.
        for table, key in (('rec_musical_sources', 'source_key'), ('recordings', 'identity_key')):
            values = []
            for operator in ('<', '>'):
                values.extend(r[0] for r in self.conn.execute(
                    f'SELECT {key} FROM {table} WHERE musical_profile_version{operator}? LIMIT ?',
                    (version, limit - len(values))))
            result.append(values)
        return tuple(result)

    @synchronized
    def queue_stale(self, version, limit=32):
        sources, keys = self.stale(version, limit)
        with self.conn.atomic():
            for key in sources:
                musical_source_queue.enqueue(self.conn, key)
            content_work.enqueue(self.conn, keys, bump=False)

    @synchronized
    def pending(self):
        row = self.conn.execute('SELECT source_key FROM rec_musical_sources WHERE pending_position IS NOT NULL '
                                'ORDER BY pending_position LIMIT 1').fetchone()
        return row[0] if row else None

    @synchronized
    def source_dependencies(self, key):
        """Bound a source publication to its current immutable metadata references."""
        dependencies = {(table, 'key:' + key) for table in (
            'rec_musical_sources', 'rec_musical_links')}
        # A format cutover changes which immutable source references are read.
        dependencies.add(('settings', 'key:' + ACTIVE))
        if metadata_runtime.active(self.conn):
            reference = metadata_native.source_reference(self.conn, key)
            dependencies.add(('collection_file_writes', 'key:' + owner_key('native', (key,))))
            return dependencies, ('runtime', reference['revision'], reference.get('file'))
        references = self.conn.execute(
            'SELECT o.id,o.delivery_id ' + metadata_native.SELECTED_OBSERVATIONS +
            'WHERE s.source_key=? ORDER BY o.field', (key,)).fetchall()
        dependencies.update(('native_metadata_observations', 'key:' + str(row[0])) for row in references)
        dependencies.update(('metadata_deliveries', 'key:' + str(row[1])) for row in references)
        return dependencies, tuple(row[0] for row in references)

    @synchronized
    def source_facts(self, key, limit=32, max_bytes=256 * 1024, *, observation_ids=None):
        with ExitStack() as guards:
            tables = ('rec_musical_sources', 'rec_musical_links')
            for table in tables:
                guards.enter_context(keyed_reads(self._store, table, [key]))
            source = self.conn.execute('SELECT provider,scope,entity_id,revision,pending_cursor,pending_position '
                                      'FROM rec_musical_sources WHERE source_key=? AND pending_position IS NOT NULL',
                                      (key,)).fetchone()
            if not source:
                return None
            if observation_ids is not None:
                if metadata_runtime.active(self.conn):
                    reference = metadata_native.source_reference(self.conn, key, store=self._store)
                    current = ('runtime', reference['revision'], reference.get('file'))
                else:
                    current = tuple(row[0] for row in self.conn.execute(
                        'SELECT f.atom FROM rec_musical_sources s JOIN json_each(s.selected_observations) f '
                        'WHERE s.source_key=? ORDER BY f.key', (key,)))
                # References may change between dependency selection and reader
                # pinning. Retry with their new scope rather than reading outside it.
                if current != observation_ids:
                    return None
            reference = metadata_native.source_reference(self.conn, key, max_bytes, store=self._store)
            ids = [r[0] for r in self.conn.execute('SELECT recording_id FROM rec_musical_links '
                'WHERE source_key=? AND recording_id>? ORDER BY recording_id LIMIT ?', (key, source['pending_cursor'], limit))]
            return ({'provider': source['provider'], 'scope': source['scope'], 'entity_id': source['entity_id'],
                     'reference': reference}, tuple(source[3:]), ids)

    @synchronized
    def publish_source(self, key, pending, ids, now, limit, *, reference):
        with self.conn.atomic():
            current = self.conn.execute('SELECT revision,pending_cursor,pending_position FROM rec_musical_sources '
                                        'WHERE source_key=? AND pending_position IS NOT NULL', (key,)).fetchone()
            if current is None or tuple(current) != pending:
                raise ValueError('Stale musical-source publication')
            profile_key, owner, publication, size, version, digest, file_id, _ = reference
            if (profile_key, owner) != (key, key):
                raise ValueError('Musical-profile owner changed')
            profile_file_writes.complete(self.conn, 'source', [reference])
            for rid in ids:
                content_work.for_recording(self.conn, rid)
            if len(ids) == limit:
                position, cursor = musical_source_queue.NEXT_POSITION, ids[-1]
                last_position = position
            else:
                position, cursor = 'NULL', None
                last_position = 'last_queue_position'
            self.conn.execute(f'''UPDATE rec_musical_sources SET musical_profile_version=?,musical_profile_digest=?,
                musical_profile_prepared_at=?,musical_profile_publication=?,musical_profile_size=?,
                musical_profile_file_id=?,pending_position={position},last_queue_position={last_position},
                pending_cursor=? WHERE source_key=?''',
                (version, digest, now, publication, size, file_id, cursor, key))

    @synchronized
    def rotate(self, key):
        with self.conn.atomic():
            position = musical_source_queue.NEXT_POSITION
            self.conn.execute(f'UPDATE rec_musical_sources SET pending_position={position},last_queue_position={position} '
                              'WHERE source_key=? AND pending_position IS NOT NULL', (key,))

    @synchronized
    def recording_facts(self, keys, *, source_scopes=None, max_sources=64, max_link_scan=MAX_MASKED_SOURCE_LINKS):
        """Bound sources; an evaluation mask precedes slots and file-byte charges.

        Masked admission inspects at most 4096 link identities per recording,
        failing closed above that bound rather than truncating allowed inputs.
        Ordinary preparation keeps its existing first-64/all-source semantics.
        """
        result = {}
        with ExitStack() as guards:
            guards.enter_context(keyed_reads(self._store, 'recordings', keys))
            for key in keys:
                recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
                if not recording:
                    result[key] = ([], {}, False)
                    continue
                rid = recording[0]
                with keyed_reads(self._store, 'rec_musical_links', [rid]):
                    links = [r[0] for r in self.conn.execute('SELECT source_key FROM rec_musical_links '
                        'WHERE recording_id=? ORDER BY source_key LIMIT ?',
                        (rid, max_sources + 1 if source_scopes is None else max_link_scan + 1))]
                if source_scopes is not None:
                    if len(links) > max_link_scan:
                        raise WorkBudgetExceeded('Masked recording source links exceed bounded admission')
                    allowed = set()
                    for start in range(0, len(links), 64):
                        batch = links[start:start + 64]
                        with keyed_reads(self._store, 'rec_musical_sources', batch):
                            allowed.update(row[0] for row in self.conn.execute(
                                'SELECT source_key,scope FROM rec_musical_sources WHERE source_key IN ('
                                + ','.join('?' for _ in batch) + ')', batch) if row[1] in source_scopes)
                    links = [link for link in links if link in allowed][:max_sources + 1]
                with keyed_reads(self._store, 'rec_musical_sources', links[:max_sources]):
                    by_key = {row[0]: row for row in profile_files.reference_rows(self.conn, 'source', links[:max_sources])}
                    references = [by_key[link] for link in links[:max_sources] if link in by_key]
                with keyed_reads(self._store, 'track_sources', [rid]):
                    entries = [r[0] for r in self.conn.execute('SELECT id FROM track_sources WHERE recording_id=?', (rid,))]
                choices = {}
                runtime = metadata_runtime.active(self.conn)
                with ExitStack() as choice_guards:
                    for table in (('track_sources',) if runtime else
                                  ('track_sources', 'metadata_observations')):
                        choice_guards.enter_context(keyed_reads(self._store, table, entries))
                    for entry in entries:
                        if runtime:
                            for field, value in self.conn.execute(
                                    'SELECT '+metadata_runtime.field_expression('c')+',json_quote('+
                                    metadata_runtime.value_expression('c')+f') FROM {runtime_metadata_choices.VIEW} c '
                                    'WHERE entry_id=? AND kind<>0 AND field<>3',
                                    (entry,)):
                                choices.setdefault(field, set()).add(value)
                        else:
                            for row in metadata_choice_selections.rows(self.conn, entry):
                                if row['value_kind'] == 'withdrawn' or row['field'] == 'energy':
                                    continue
                                value = metadata_evidence.value_from_row(row).selected()
                                choices.setdefault(row['field'], set()).add(json.dumps(value))
                result[key] = (references, choices, len(links) > max_sources)
        return result

    @synchronized
    def publish_recordings(self, rows, now, *, references=()):
        with self.conn.atomic():
            changed = False
            prepared = {reference[0]: reference for reference in references}
            publishing_files = []
            for key, payload, version, digest in rows:
                if payload is None:
                    cursor = self.conn.execute('''UPDATE recordings SET musical_profile_version=NULL,
                        musical_profile_digest=NULL,musical_profile_prepared_at=NULL,musical_profile_publication=NULL,
                        musical_profile_size=NULL,musical_profile_file_id=NULL
                        WHERE identity_key=? AND musical_profile_version IS NOT NULL''', (key,))
                else:
                    reference = prepared[key]
                    _, recording, publication, size, file_version, file_digest = reference[:6]
                    if (file_version, file_digest) != (version, digest):
                        raise ValueError('Stale musical-profile publication')
                    cursor = self.conn.execute('''UPDATE recordings SET musical_profile_version=?,
                        musical_profile_digest=?,musical_profile_prepared_at=?,musical_profile_publication=?,
                        musical_profile_size=?,musical_profile_file_id=? WHERE id=? AND identity_key=?
                        AND (musical_profile_version IS NOT ? OR musical_profile_digest IS NOT ?)''',
                        (version, digest, now, publication, size, reference[6], recording, key, version, digest))
                    if cursor.rowcount:
                        publishing_files.append(reference)
                    elif not self.conn.execute('SELECT 1 FROM recordings WHERE id=? AND identity_key=?',
                                               (recording, key)).fetchone():
                        raise ValueError('Musical-profile owner changed')
                changed = changed or cursor.rowcount > 0
            profile_file_writes.complete(self.conn, 'recording', publishing_files)
        return changed

    @synchronized
    def assert_idle(self):
        if self.conn.in_transaction:
            raise RuntimeError('Musical-profile files must be prepared outside a transaction')

    @synchronized
    def publication_owners(self, kind, keys):
        return {key: profile_files._identity(self.conn, kind, key) for key in keys}

    @synchronized
    def reserve_files(self, records):
        if not records:
            return []
        with self.conn.atomic():
            return profile_file_writes.reserve(self.conn, records)

    @synchronized
    def file_required(self, kind, reference):
        if kind not in profile_files.KINDS:
            raise ValueError('Invalid profile kind')
        table = 'recordings' if kind == 'recording' else 'rec_musical_sources'
        clause = ('musical_profile_file_id=?' if type(reference) is int else
                  'musical_profile_publication=? AND musical_profile_file_id IS NULL')
        return self.conn.execute(f'SELECT 1 FROM {table} WHERE {clause} LIMIT 1',
                                 (reference,)).fetchone() is not None

    @snapshot_read
    def readiness_rows(self, keys, *, patterns):
        """Read the current ranking and support for the prepared candidates."""
        if not keys:
            return []
        qs = ','.join('?' for _ in keys)
        with column_group_reads(self._store, 'recordings', 'identity'):
            ids = [row[0] for row in self.conn.execute(
                f'SELECT id FROM recordings WHERE identity_key IN ({qs})', keys)]
        marks = ','.join('?' for _ in ids)
        with keyed_reads(self._store, 'recordings', keys), \
                keyed_reads(self._store, 'recording_vibes', ids), \
                keyed_reads(self._store, 'vibe_versions', []):
            return [tuple(row) for row in self.conn.execute(
                'SELECT r.identity_key,r.musical_profile_version,r.musical_profile_digest,'
                'v.vibe,v.score,v.support,v.source,v.inputs_version FROM recordings r '
                f'LEFT JOIN (SELECT * FROM served_vibe_fits WHERE recording_id IN ({marks}) AND '
                + score_version_filter() + ') v ON v.recording_id=r.id '
                + f' WHERE r.identity_key IN ({qs}) ORDER BY r.identity_key,v.vibe', (*ids, *patterns, *keys)).fetchall()]

    @snapshot_read
    def profile_references(self, keys):
        with keyed_reads(self._store, 'recordings', keys):
            return profile_files.reference_rows(self.conn, 'recording', keys)

    @snapshot_read
    def provenance_references(self, key, digests, *, limit=16):
        if not digests:
            return []
        qs = ','.join('?' for _ in digests)
        rows = self.conn.execute(
            'SELECT p.source_key '
            'FROM recordings r JOIN rec_musical_links l ON l.recording_id=r.id '
            'JOIN rec_musical_sources p ON p.source_key=l.source_key '
            f'WHERE r.identity_key=? AND p.musical_profile_digest IN ({qs}) ORDER BY p.source_key LIMIT ?',
            (key, *digests, limit + 1)).fetchall()
        references = {row[0]: row for row in profile_files.reference_rows(self.conn, 'source', [row[0] for row in rows])}
        return [references[source_key] for source_key, in rows]
