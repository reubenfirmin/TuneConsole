"""Progressive content preparation using the established encoder and clustering.

Accepted per-recording inputs form a durable corpus. A basis fit consumes that
corpus, not live provider rows; its publication still uses strict snapshot
guards. Every vector in a publication uses the exact same encoder, including
its audio normalization. Last-good models remain available between steps.
"""
from dataclasses import dataclass
import json
import math
import sys
import uuid

from yt_playlist.core.activity import check_stopping, work_budget, WorkBudgetExceeded
from yt_playlist.rec import content_model, group_assignment, group_preparation, musical_preparation, rec_params, taste_modes
from yt_playlist.rec import vibe_preparation
from yt_playlist.repos.rec_model import VectorChanges
from yt_playlist.util import genre_catalog

PHASE = 'rec: progressive content basis'
INPUT_PHASE = 'rec: progressive content inputs'
BASIS_STATE = 'rec_preparation_basis'
FULL_STATE = 'rec_preparation_full'
FIT_STATE = 'rec_preparation_fit'
MAINTENANCE_INTERVAL = 15 * 60
MODE_PARAMS = ('modes_k', 'modes_min_members', 'modes_match_threshold')
# Repair reads only these whole-table anti-joins. Capture their counters rather
# than copying every per-record revision in the application for each small slice.
REPAIR_INPUTS = frozenset((table, '') for table in (
    'recordings', 'rec_content_vectors', 'rec_discovered_content_vectors',
    'rec_prepared_content', 'rec_content_pending'))


@dataclass(frozen=True)
class Budget:
    batch: int = 32
    capture_seconds: float = 2
    fit_seconds: float = 10
    max_rows: int = 32768
    memory_bytes: int = 128 * 1024 * 1024
    full_memory_bytes: int = 512 * 1024 * 1024
    fit_interval: float = 60


def _state(store, key):
    return json.loads(store.settings.get_setting(key) or '{}')


def _threshold(size, *, minimum=4):
    return max(minimum, min(128, math.ceil(size / 10)))


def _config(store):
    return [store.settings.get_setting(key) for key in
            ('rec_embed_method', 'rec_dim', 'rec_cooc_weighting', 'rec_selection_restore',
             *(rec_params.SETTING_PREFIX + name for name in MODE_PARAMS))]


def _object_bytes(value):
    """Conservative size of our acyclic JSON/model inputs.

    Shared values count again. Avoid a global identity set whose own allocation
    could exceed the budget while estimating a large corpus.
    """
    pending, visited, size = [value], 0, 0
    while pending:
        if visited % 256 == 0:
            check_stopping()
        item = pending.pop()
        visited += 1
        size += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
    return size


def _fit_memory(corpus, inputs, discoveries, model, genres, k, previous_bytes):
    """Conservative phase peaks for the encoder and unchanged float64 clustering.

    This bounds admitted working data, not the whole application's RSS. Eight
    MiB covers small arrays, model metadata and allocator overhead. Cluster
    member copies, distance chunks and old publication vectors are explicit.
    """
    rows = len(set(inputs.content) | set(inputs.audio))
    dim = model['ncat'] + len(model['cont'])
    matrix = rows * dim * 4
    serialized = (rows + len(discoveries)) * (dim * 4 + 512)
    metadata = rows * 256 + _object_bytes((model, genres))
    allowance = 8 * 1024 * 1024
    encoding = (_object_bytes((corpus, inputs, discoveries, model, genres)) + matrix + serialized
                + dim * 16 + allowance)
    k = min(int(k), rows)
    distance = 2 * max(taste_modes._DISTANCE_WORK_BYTES, k * dim * 8)
    clustering = (serialized + metadata + matrix + rows * dim * 16
                  + distance + rows * 128 + k * dim * 32 + allowance)
    publication = serialized + metadata + previous_bytes + allowance
    phases = {'encoding': encoding, 'clustering': clustering, 'publication': publication}
    return max(phases.values()), phases


def maintenance_view(store):
    """Cheap persisted status; viewing the page never queues or performs fitting."""
    state = _state(store, FIT_STATE)
    blocked = (state.get('reason') in {'matrix-memory-budget', 'corpus-memory-budget'}
               or (state.get('reason') == 'fit-time-budget' and state.get('attempts', 0) >= 3))
    return {**state, 'blocked': blocked,
            'has_model': bool(store.settings.get_setting('rec_content_model'))}


def _delta(reader, rows, raw_model):
    """Serialize bounded changes against one already published, fixed basis."""
    if not raw_model:
        return {}
    model = json.loads(raw_model)
    keys = [r[0] for r in rows]
    pools = reader.rec_model.content_vector_pools(keys)
    changes = {}
    for scope, table in (('library', 'rec_content_vectors'), ('discovered', 'rec_discovered_content_vectors')):
        previous = pools[scope]
        upserts, removed = [], []
        for key, _revision, raw, _digest, _different in rows:
            check_stopping()
            features = json.loads(raw)[scope]
            vector = (content_model.encode_content(model, features['genre'], features['year'], features['audio'])
                      if features else None)
            blob = vector.tobytes() if vector is not None else None
            if blob is not None and previous.get(key) != blob:
                upserts.append((key, blob))
            elif blob is None and key in previous:
                removed.append(key)
        if upserts or removed:
            changes[table] = VectorChanges(tuple(upserts), tuple(removed))
    return changes


class Preparation:
    def __init__(self, store, *, budget=Budget()):
        self.store, self.budget = store, budget
        self.batch = budget.batch
        self.retry_fit_at = _state(store, FIT_STATE).get('next_attempt_at', 0)
        self.next_maintenance_at = 0
        self._repair_needed = False
        self._source_turn = True
        self._vibe_turn = False
        self.groups = group_preparation.Preparation(store)

    def _record(self, outcome, reason, *, phase=PHASE):
        self.store.maintenance_diagnostics.record(outcome, reasons=(reason,), phase=phase)

    def _defer(self, now, reason, *, outcome='deferred', **details):
        previous = _state(self.store, FIT_STATE)
        attempts = previous.get('attempts', 0) + 1 if previous.get('reason') == reason else 1
        delay = (MAINTENANCE_INTERVAL if reason in {'matrix-memory-budget', 'corpus-memory-budget'}
                 or (reason == 'fit-time-budget' and attempts >= 3)
                 else self.budget.fit_interval)
        self.retry_fit_at = now + delay
        state = {'state': outcome, 'reason': reason, 'attempted_at': now, 'attempts': attempts,
                 'since': previous.get('since', now) if previous.get('reason') == reason else now,
                 'next_attempt_at': self.retry_fit_at, 'memory_bytes': self.budget.memory_bytes,
                 **details}
        self.store.settings.set_setting(FIT_STATE, json.dumps(state))
        self._record(outcome, reason)

    def _maintenance_due(self, now):
        if now >= self.next_maintenance_at and not self._repair_needed:
            self.next_maintenance_at = now + MAINTENANCE_INTERVAL
            try:
                with work_budget(self.budget.capture_seconds), self.store.read_view() as reader:
                    self._repair_needed = bool(reader.prepared_content.missing_unqueued(1))
            except WorkBudgetExceeded:
                self.next_maintenance_at = now + self.budget.fit_interval
                self._record('deferred', 'input-audit-budget', phase=INPUT_PHASE)
        return self._repair_needed

    def repair(self, now):
        """One bounded audit/repair slice in the existing preparation worker job."""
        if not self._maintenance_due(now):
            return False
        try:
            with work_budget(self.budget.capture_seconds), self.store.read_snapshot(
                    dependencies=REPAIR_INPUTS) as snapshot:
                keys = snapshot.store.prepared_content.missing_unqueued(self.budget.batch)
                snapshot.release_reader()
                if not snapshot.publish(lambda: self.store.prepared_content.enqueue_missing(keys)):
                    return False
            self._repair_needed = len(keys) == self.budget.batch
            self.next_maintenance_at = now + MAINTENANCE_INTERVAL
            if keys:
                self._record('published', 'missing-inputs-requeued', phase=INPUT_PHASE)
            return bool(keys)
        except WorkBudgetExceeded:
            self._record('deferred', 'input-repair-budget', phase=INPUT_PHASE)
            return False

    def _basis_reason(self, now):
        previous = _state(self.store, BASIS_STATE)
        changed = self.store.prepared_content.changed_since(previous.get('serial', 0))
        if not self.store.settings.get_setting('rec_content_model'):
            return 'initial-evidence' if self.store.prepared_content.changed_since(-1) else None
        if self.store.settings.get_setting('rec_content_genre_revision') != genre_catalog.REVISION:
            return 'genre-revision'
        if previous and previous.get('modes') != [rec_params.get_param(self.store, key) for key in MODE_PARAMS]:
            return 'grouping-settings'
        if not changed:
            return None
        # A small basis is cheap and must learn from a single useful recording.
        # The larger collaborative fit retains its four-change floor.
        if changed >= _threshold(previous.get('rows', 0), minimum=1):
            return 'accumulated-evidence'
        if now - previous.get('at', now) >= 900:
            return 'aged-evidence'
        return None

    def due(self, now):
        """Retry a deferred basis even after the durable input queue has drained."""
        return (self._maintenance_due(now) or self.store.prepared_content.has_pending()
                or musical_preparation.due(self.store)
                or vibe_preparation.due(self.store)
                or (now >= self.retry_fit_at and self._basis_reason(now) is not None))

    def capture(self, now):
        # The audit is periodic even without new catalog writes. A lost queue
        # entry cannot permanently strand an otherwise healthy installation.
        self.repair(now)
        # Alternate source fan-out and recording captures. Each admission gets
        # one time budget, and continual source updates cannot starve recordings.
        if self._source_turn:
            self._source_turn = False
            self._vibe_turn = not self._vibe_turn
            if not self._vibe_turn and vibe_preparation.step(self.store, self.batch):
                return True
            if musical_preparation.source_step(self.store, now, batch=self.batch,
                                               seconds=self.budget.capture_seconds):
                return True
            if self.store.musical_profiles.pending():
                return False
        self._source_turn = True
        pending = self.store.prepared_content.pending(self.batch)
        if not pending:
            return vibe_preparation.step(self.store, self.batch)
        try:
            with work_budget(self.budget.capture_seconds), self.store.read_snapshot() as snapshot:
                reader = snapshot.store
                rows = reader.prepared_content.capture(pending)
                source_inputs = reader.musical_profiles.recording_inputs([row[0] for row in rows])
                musical = musical_preparation.recordings(reader, rows, inputs=source_inputs)
                musical, vibe_updates = vibe_preparation.recordings(reader, rows, musical, source_inputs=source_inputs)
                assignments = group_assignment.prepare(reader, rows, musical, now)
                raw_model = reader.settings.get_setting('rec_content_model')
                revision = reader.settings.get_setting('rec_content_genre_revision')
                changes = _delta(reader, rows, raw_model if revision == genre_catalog.REVISION else None)
                # Guard a competing publisher even when this batch has no vector changes.
                reader.settings.get_setting('rec_content_generation')
                def publish():
                    self.store.prepared_content.publish_capture(rows, now)
                    self.store.musical_profiles.publish_recordings(musical, now)
                    self.store.vibe_predictions.publish(*vibe_updates, now)
                    self.store.taste_groups.publish_assignments(assignments)
                    if changes:
                        self.store.rec_model.publish_training_generation(changes)
                        self.store.settings.publish_values({
                            'rec_content_generation': uuid.uuid4().hex,
                            'rec_content_inputs': '',  # not a claim of a current full-corpus fit
                        })
                if snapshot.publish(publish):
                    self.batch = min(self.budget.batch, max(1, self.batch * 2))
                    self._record('published', 'changed-entity-inputs', phase=INPUT_PHASE)
                    return True
        except WorkBudgetExceeded:
            self._record('deferred', 'capture-budget', phase=INPUT_PHASE)
        self.batch = max(1, self.batch // 2)
        self.store.prepared_content.rotate(pending)
        self._record('deferred', 'hot-input-batch', phase=INPUT_PHASE)
        return False

    def fit(self, now):
        """Fit a coherent basis only after useful accumulated changes, within a budget."""
        if now < self.retry_fit_at:
            return False
        reason = self._basis_reason(now)
        if reason is None:
            return False
        self.retry_fit_at = now + self.budget.fit_interval
        try:
            with work_budget(self.budget.fit_seconds), self.store.read_snapshot() as snapshot:
                reader = snapshot.store
                raw_model = reader.settings.get_setting('rec_content_model')
                if raw_model and reader.prepared_content.missing_previous_inputs():
                    self._defer(now, 'previous-generation-inputs', outcome='waiting')
                    return False
                corpus = reader.prepared_content.corpus(self.budget.max_rows)
                if corpus is None:
                    self._defer(now, 'corpus-memory-budget', max_rows=self.budget.max_rows)
                    return False
                content, audio, keys, discoveries = {}, {}, [], []
                for key, payload, _digest in corpus:
                    check_stopping()
                    features = payload['library']
                    if features is not None:
                        keys.append(key)
                        if features['genre'] or features['year']:
                            content[key] = (features['genre'], features['year'])
                        if features['audio']:
                            audio[key] = features['audio']
                    if payload['discovered'] is not None:
                        discoveries.append({'identity_key': key, **payload['discovered']})
                if not content and not audio:
                    self._defer(now, 'empty-corpus', outcome='waiting')
                    return False
                model = content_model.build_content_model(content, audio)
                dim = model['ncat'] + len(model['cont'])
                inputs = content_model.ContentInputs(keys, content, audio)
                genres = {key: value[0] for key, value in content.items()}
                estimated, phases = _fit_memory(corpus, inputs, discoveries, model, genres,
                    rec_params.get_param(reader, 'modes_k'), reader.prepared_content.vector_storage_bytes())
                rows = len(corpus)
                if estimated > self.budget.memory_bytes:
                    self._defer(now, 'matrix-memory-budget', rows=rows, dimensions=dim,
                                estimated_bytes=estimated, phase_bytes=phases)
                    return False
                fingerprint = content_model.input_fingerprint(inputs)
                encoded_keys, matrix, model = content_model.build_library_content(inputs)
                generation = {'model': json.dumps(model), 'generation': uuid.uuid4().hex,
                    'revision': genre_catalog.REVISION, 'inputs': fingerprint,
                    'library': [(key, matrix[i].tobytes()) for i, key in enumerate(encoded_keys)],
                    'discovered': content_model.encode_discovered(discoveries, model)}
                # Clustering retains the encoded matrix and genre labels, not
                # the decoded corpus or audio/input dictionaries.
                del corpus, inputs, content, audio, keys, discoveries
                reader.rec_model.content_generation(keys=[])  # competing model/basis publisher guard
                modes = taste_modes.prepare_modes(reader, now, snapshot=generation,
                    genres=genres, vector_space=(encoded_keys, matrix, {}))
                del matrix, genres
                changes = reader.rec_model.prepare_training_changes({
                    'rec_content_vectors': generation['library'],
                    'rec_discovered_content_vectors': generation['discovered']})
                serial = reader.prepared_content.serial()
                state = {'serial': serial, 'rows': rows, 'at': now, 'reason': reason,
                         'modes': [rec_params.get_param(reader, key) for key in MODE_PARAMS],
                         'estimated_bytes': estimated, 'phase_bytes': phases,
                         'dimensions': dim, 'input_fingerprint': generation['inputs']}
                settings = {'rec_content_model': generation['model'],
                            'rec_content_generation': generation['generation'],
                            'rec_content_genre_revision': generation['revision'],
                            'rec_content_inputs': generation['inputs'], BASIS_STATE: json.dumps(state),
                            FIT_STATE: json.dumps({'state': 'ready', 'attempted_at': now,
                                'estimated_bytes': estimated, 'memory_bytes': self.budget.memory_bytes})}
                if modes is not None:
                    upserts, retired = modes
                    modes = ([(m['mode_id'], m['label'], json.dumps(m['families']), m['centroid'].tobytes(),
                               m['size'], json.dumps(m['rep_keys']), now, now, m['space']) for m in upserts], retired)
                def publish():
                    self.store.rec_model.publish_training_generation(changes)
                    self.store.settings.publish_values(settings)
                    if modes is not None:
                        self.store.modes.publish_generation(*modes)
                if snapshot.publish(publish):
                    self._record('published', reason)
                    return True
                self._defer(now, 'prepared-corpus-changed')
        except WorkBudgetExceeded:
            self._defer(now, 'fit-time-budget')
        return False

    def full_due(self, now=None):
        """A metadata write alone cannot admit an expensive complete model rebuild."""
        previous = _state(self.store, FULL_STATE)
        config = _config(self.store)
        if (not previous or config != previous.get('config')
                or not self.store.settings.get_setting('rec_fit_inputs')
                or not self.store.settings.get_setting('rec_artist_fit_inputs')):
            return True
        _content, evidence = self.store.prepared_content.counters()
        changed = self.store.prepared_content.changed_since(previous['content'])
        evidence_changed = evidence - previous['evidence']
        due = (evidence_changed >= 4 or changed >= _threshold(previous['rows'])
               or (now is not None and (changed or evidence_changed)
                   and now - (previous['at'] if previous.get('at') is not None else now) >= 900))
        if not due and (changed or evidence_changed):
            self._record('waiting', 'accumulating-evidence')
        return bool(due)

    def full_checkpoint(self, now=None):
        content, evidence = self.store.prepared_content.counters()
        return {
            'content': content, 'evidence': evidence, 'at': now,
            'rows': self.store.prepared_content.changed_since(-1),
            'config': _config(self.store)}

    def completed_full(self, checkpoint):
        self.store.settings.set_setting(FULL_STATE, json.dumps(checkpoint))

    def status(self):
        """Bounded operational progress, separate from listener-facing setup copy."""
        return {**self.store.prepared_content.progress(),
                'basis': _state(self.store, BASIS_STATE),
                'full': _state(self.store, FULL_STATE),
                'fit': _state(self.store, FIT_STATE),
                'budget': vars(self.budget)}
