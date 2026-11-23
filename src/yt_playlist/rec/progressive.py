"""Progressive content preparation using the established encoder.

Accepted per-recording inputs form a durable corpus. A basis fit consumes that
corpus, not live provider rows; its publication still uses strict snapshot
guards. Every vector in a publication uses the exact same encoder, including
its audio normalization. Last-good models remain available between steps.
"""
from dataclasses import dataclass
from contextlib import closing
import json
import logging
import math
import sys
import time
import uuid

from yt_playlist.core.activity import check_stopping, WorkBudgetExceeded
from yt_playlist.core import content_work, embeddings
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.model_state import RecordingModelUpdate
from yt_playlist.controllers.recommendation_refresh import VERSION as REFRESH_VERSION
from yt_playlist.rec import content_character, content_model, group_preparation, musical_preparation
from yt_playlist.rec import vibe_preparation, vibe_joint_preparation
from yt_playlist.core.rec_model_policy import VectorChanges
from yt_playlist.util import genre_catalog

log = logging.getLogger(__name__)

PHASE = 'rec: progressive content basis'
INPUT_PHASE = 'rec: progressive content inputs'
BASIS_STATE = 'rec_preparation_basis'
FULL_STATE = 'rec_preparation_full'
FIT_STATE = 'rec_preparation_fit'
MAINTENANCE_INTERVAL = 15 * 60
# Repair reads only these whole-table anti-joins. Capture their counters rather
# than copying every per-record revision in the application for each small slice.
REPAIR_INPUTS = frozenset({('recordings', ''), ('prepared_recording_inputs', 'group:repair'),
                           ('settings', 'key:' + content_work.SEQUENCE)})


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
            ('rec_embed_method', 'rec_dim', 'rec_cooc_weighting', 'rec_selection_restore')]


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


def _fit_memory(corpus, inputs, discoveries, model, previous_bytes):
    """Conservative working-set estimates for encoding and publication."""
    rows = len(set(inputs.content) | set(inputs.audio))
    dim = model['ncat'] + len(model['cont'])
    matrix = rows * dim * 4
    serialized = (rows + len(discoveries)) * (dim * 4 + 512)
    metadata = rows * 256 + _object_bytes(model)
    allowance = 8 * 1024 * 1024
    encoding = (_object_bytes((corpus, inputs, discoveries, model)) + matrix + serialized
                + dim * 16 + allowance)
    publication = serialized + metadata + previous_bytes + allowance
    phases = {'encoding': encoding, 'publication': publication}
    return max(phases.values()), phases


def maintenance_view(store):
    """Cheap persisted status; viewing the page never queues or performs fitting."""
    state = _state(store, FIT_STATE)
    return maintenance_values(state, bool(store.rec_model.recording_encoder()))


def maintenance_values(state, has_model):
    blocked = (state.get('reason') in {'matrix-memory-budget', 'corpus-memory-budget'}
               or (state.get('reason') == 'fit-time-budget' and state.get('attempts', 0) >= 3))
    return {**state, 'blocked': blocked,
            'has_model': has_model}


def _delta(reader, decoded, raw_model, character):
    """Serialize bounded changes against one already published, fixed basis."""
    if not raw_model:
        return {}
    if not reader.rec_model.content_generation(keys=[])['coherent']:
        return {}
    keys = [key for key, _payload in decoded]
    pools = reader.rec_model.content_vector_pools(keys)
    axes = reader.genres.feature_axes(features['genre'] for _key,payload in decoded
                                      for features in payload.values() if isinstance(features,dict) and 'genre' in features)
    return reader.rec_model.prepare_training_deltas(content_deltas(decoded, raw_model, pools, axes, character))


def content_deltas(decoded, raw_model, pools, axes, character=None):
    """Calculate additions/removals in a bounded page against its fixed encoder."""
    model = json.loads(raw_model)
    changes = {}
    for scope, table in (('library', 'rec_content_vectors'), ('discovered', 'rec_discovered_content_vectors')):
        previous = pools[scope]
        upserts, removed = [], []
        for key, payload in decoded:
            check_stopping()
            features = payload[scope]
            vector = None
            if features:
                audio = features['audio']
                if model.get('policy') and character is not None:
                    audio = content_character.audio(audio, character[key][0])
                vector = content_model.encode_content(model, features['genre'], features['year'], audio, genre_axes=axes)
            blob = vector.tobytes() if vector is not None else None
            if blob is not None and previous.get(key) != blob:
                upserts.append((key, blob))
            elif blob is None and key in previous:
                removed.append(key)
        if upserts or removed:
            changes[table] = VectorChanges(tuple(upserts), tuple(removed),
                model_version=embeddings.version(table, {'rec_content_model': raw_model}))
    return changes


def basis_reason(previous, *, changed, has_model, has_evidence, coherent, genre_revision, now):
    if not has_model:
        return 'initial-evidence' if has_evidence else None
    if not coherent:
        return 'encoder-repair'
    if genre_revision != genre_catalog.REVISION:
        return 'genre-revision'
    if not changed:
        return None
    if changed >= _threshold(previous.get('rows', 0), minimum=1):
        return 'accumulated-evidence'
    if now - previous.get('at', now) >= 900:
        return 'aged-evidence'
    return None


def full_due_values(previous, config, fitted, artist_fitted, changed, evidence_changed, now):
    saved = previous.get('config')
    if saved is None or config[:4] != saved[:4] or not fitted or not artist_fitted:
        return True
    return bool(evidence_changed >= 4 or changed >= _threshold(previous['rows'])
                or (now is not None and (changed or evidence_changed)
                    and now - (previous['at'] if previous.get('at') is not None else now) >= 900))


def corpus_inputs(corpus, character=None):
    content, audio, keys, discoveries = {}, {}, [], []
    for key, payload, _digest in corpus:
        check_stopping()
        features = payload['library']
        if features is not None:
            keys.append(key)
            if features['genre'] or features['year']:
                content[key] = (features['genre'], features['year'])
            catalog = features['audio']
            if character is not None:
                if catalog or character[key][0]:
                    audio[key] = content_character.audio(catalog, character[key][0])
            elif catalog:
                audio[key] = catalog
        if payload['discovered'] is not None:
            row = {'identity_key': key, **payload['discovered']}
            if character is not None:
                row['character'] = character[key][0]
            discoveries.append(row)
    policy = content_character.VERSION if character is not None else None
    return content_model.ContentInputs(keys, content, audio, policy), discoveries


def encode_basis(inputs, discoveries, axes, generation):
    fingerprint = content_model.input_fingerprint(inputs, genre_axes=axes)
    keys, matrix, model = content_model.build_library_content(inputs, genre_axes=axes)
    return {'model': json.dumps(model), 'generation': generation,
            'revision': genre_catalog.REVISION, 'inputs': fingerprint,
            'library': [(key, matrix[i].tobytes()) for i, key in enumerate(keys)],
            'discovered': content_model.encode_discovered(discoveries, model, genre_axes=axes)}


class SourceTurns:
    """Fair bounded source/recording admissions, independent of persistence."""
    def __init__(self):
        self.source_turn, self.vibe_turn = True, False

    def capture(self, commands, now):
        if commands.capturing():
            return commands.advance()
        commands.repair(now)
        if self.source_turn:
            self.source_turn = False
            self.vibe_turn = not self.vibe_turn
            if not self.vibe_turn and commands.vibe():
                return True
            if commands.musical(now):
                return True
            if commands.musical_pending():
                return False
        self.source_turn = True
        if pending := commands.pending():
            return commands.start(pending, now)
        return commands.vibe()


class Preparation:
    def __init__(self, store, *, budget=Budget()):
        self.store, self.budget = store, budget
        self.batch = budget.batch
        self.retry_fit_at = _state(store, FIT_STATE).get('next_attempt_at', 0)
        self.next_maintenance_at = 0
        self._repair_needed = False
        self._repair_after = None
        self.turns = SourceTurns()
        self._capturing = None
        self._capturing_pending = None
        self._capturing_expires_at = 0
        self._capturing_kind = None
        self.groups = group_preparation.Preparation(store)

    def close(self):
        if self._capturing is not None:
            try:
                self._capturing.close()
            finally:
                self._capturing = None
                self._capturing_pending = None
                self._capturing_expires_at = 0
                self._capturing_kind = None

    def expire_capture(self, *, paused=False):
        """Release idle snapshot leases even when no further work is admitted.

        The worker serializes this with capture. The original pending queue
        needs no repair: only complete guarded publication acknowledges it.
        """
        if self._capturing is not None and (paused or time.monotonic() >= self._capturing_expires_at):
            self.close()

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
                with self.store.read_view() as reader:
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
            with self.store.read_snapshot(
                    dependencies=REPAIR_INPUTS) as snapshot:
                keys = snapshot.store.prepared_content.missing_unqueued(
                    self.budget.batch, after=self._repair_after)
                snapshot.release_reader()
                if not snapshot.publish(lambda: self.store.prepared_content.enqueue_missing(keys)):
                    self._repair_after = None
                    return False
            self._repair_needed = len(keys) == self.budget.batch
            # A new audit starts at the beginning, including lost work behind
            # this cursor. Advance only after a guarded, successful enqueue.
            self._repair_after = keys[-1] if self._repair_needed else None
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
        has_model = bool(self.store.rec_model.recording_encoder())
        return basis_reason(previous, changed=changed, has_model=has_model,
            has_evidence=bool(self.store.prepared_content.changed_since(-1)) if not has_model else False,
            coherent=self.store.rec_model.content_generation(keys=[])['coherent'] if has_model else True,
            genre_revision=self.store.rec_model.recording_state().genre_revision if has_model else None,
            now=now)

    def due(self, now):
        """Retry a deferred basis even after the durable input queue has drained."""
        return (self._capturing is not None or self._maintenance_due(now) or self.store.prepared_content.has_pending()
                or musical_preparation.due(self.store)
                or vibe_preparation.due(self.store)
                or (now >= self.retry_fit_at and self._basis_reason(now) is not None))

    def capture(self, now):
        from yt_playlist.library.preparation_composition import source_commands
        return self.turns.capture(source_commands(self), now)

    def _start_capture(self, pending, now):
        self._capturing_pending = pending
        self._capturing_kind = 'recordings'
        self._capturing_expires_at = (time.monotonic() + self.budget.capture_seconds
            + len(vibe_joint_preparation.VIBES) * self.budget.fit_seconds)
        self._capturing = self._capture(pending, now)
        return self._advance_capture()

    def _joint_step(self, *, waiting=False):
        self._capturing_kind = 'joint'
        self._capturing_pending = None
        # This bounds the lifetime of the pinned reader, not one worker turn.
        self._capturing_expires_at = time.monotonic() + 180
        self._capturing = vibe_joint_preparation.steps(self.store,
            seconds=self.budget.fit_seconds, input_seconds=self.budget.capture_seconds,
            memory_bytes=self.budget.memory_bytes, waiting=waiting)
        published = self._advance_capture()
        return published or self._capturing is not None

    def _advance_capture(self):
        try:
            # The generator's units are memory/row bounded. Complete the
            # admitted recording/channel batch rather than expiring a pinned
            # generator and repeating every already calculated channel.
            while True:
                check_stopping()
                next(self._capturing)
        except StopIteration as completed:
            published = bool(completed.value)
        except WorkBudgetExceeded:
            self._record('deferred', 'capture-budget', phase=INPUT_PHASE)
            published = False
        except BaseException:
            self.close()
            raise
        pending = self._capturing_pending
        kind = self._capturing_kind
        self.close()
        if kind == 'joint':
            return published
        if published:
            self.batch = min(self.budget.batch, max(1, self.batch * 2))
            self._record('published', 'changed-entity-inputs', phase=INPUT_PHASE)
            return True
        self.batch = max(1, self.batch // 2)
        self.store.prepared_content.rotate(pending)
        self._record('deferred', 'hot-input-batch', phase=INPUT_PHASE)
        return False

    def _capture(self, pending, now):
        with self.store.read_snapshot() as snapshot:
            reader = snapshot.store
            rows, previous = reader.prepared_content.capture_with_previous(pending)
            source_inputs = reader.musical_profiles.recording_inputs([row[0] for row in rows])
            musical = musical_preparation.recordings(rows, inputs=source_inputs)
            musical, vibe_updates = yield from vibe_preparation.recording_steps(
                reader, rows, musical, source_inputs=source_inputs)
            raw_model = reader.rec_model.recording_encoder()
            # Reading state also guards a competing publisher when this batch changes no vectors.
            revision = reader.rec_model.recording_state().genre_revision
            decoded = [(key, json.loads(raw)) for key, _revision, raw, _digest, _different in rows]
            character = {key: content_character.evidence(json.loads(payload)) if payload else ({}, {})
                         for key, payload, _version, _digest in musical}
            accepted = []
            for key, position, raw, catalog_digest, _different in rows:
                digest = (content_character.fingerprint(catalog_digest, character[key])
                          if any(character[key]) else catalog_digest)
                accepted.append((key, position, raw, digest, previous.get(key) != digest))
            changes = _delta(reader, decoded, raw_model if revision == genre_catalog.REVISION else None, character)
            stage_started = time.monotonic()
            vibe_plan = reader.vibe_predictions.prepare_publication(*vibe_updates, now)
            def publish():
                self.store.musical_profiles.publish_recordings(musical, now)
                self.store.vibe_predictions.publish_prepared(vibe_plan)
                if changes:
                    self.store.rec_model.publish_training_generation(changes)
                    # A partial update invalidates the full-corpus fingerprint.
                    self.store.rec_model.publish_model_state(recording=RecordingModelUpdate(
                        generation=uuid.uuid4().hex, input_fingerprint=''))
                self.store.prepared_content.publish_capture(accepted, now)
            with closing(vibe_plan):
                publish_started = time.monotonic()
                published = snapshot.publish_ordered(publish)
                log.debug('Vibe recording publication recordings=%d published=%s stage_seconds=%.6f publication_seconds=%.6f',
                    len(rows), published, publish_started - stage_started, time.monotonic() - publish_started)
                return published

    def fit(self, now):
        """Fit a coherent basis only after useful accumulated changes, within a budget."""
        if self._capturing is not None:
            return False
        if now < self.retry_fit_at:
            return False
        reason = self._basis_reason(now)
        if reason is None:
            return False
        self.retry_fit_at = now + self.budget.fit_interval
        try:
            with self.store.read_snapshot() as snapshot:
                reader = snapshot.store
                raw_model = reader.rec_model.recording_encoder()
                if raw_model and reader.prepared_content.missing_previous_inputs():
                    self._defer(now, 'previous-generation-inputs', outcome='waiting')
                    return False
                corpus = reader.prepared_content.corpus(self.budget.max_rows, max_bytes=self.budget.memory_bytes)
                if corpus is None:
                    self._defer(now, 'corpus-memory-budget', max_rows=self.budget.max_rows)
                    return False
                catalogs = {key: features['audio'] for key, payload, _digest in corpus
                            if (features := payload['library'] or payload['discovered']) is not None}
                # The accepted corpus already supplies catalog inputs. Only the
                # published profile references and their overlays enter this fit.
                with column_group_reads(reader, 'recordings', 'content_profile'):
                    character = content_character.load(reader, catalogs)
                # Retracted rows only preserve a revision marker.
                inputs, discoveries = corpus_inputs(corpus, character)
                del character, catalogs
                keys, content, audio = inputs.keys, inputs.content, inputs.audio
                if not content and not audio:
                    self._defer(now, 'empty-corpus', outcome='waiting')
                    return False
                axes = reader.genres.feature_axes([*(value[0] for value in content.values()),
                                                  *(row.get('genre') for row in discoveries)])
                model = content_model.build_content_model(content, audio, genre_axes=axes, policy=inputs.policy)
                dim = model['ncat'] + len(model['cont'])
                estimated, phases = _fit_memory(corpus, inputs, discoveries, model,
                    reader.prepared_content.vector_storage_bytes())
                rows = len(corpus)
                if estimated > self.budget.memory_bytes:
                    self._defer(now, 'matrix-memory-budget', rows=rows, dimensions=dim,
                                estimated_bytes=estimated, phase_bytes=phases)
                    return False
                generation = encode_basis(inputs, discoveries, axes, uuid.uuid4().hex)
                del corpus, inputs, content, audio, keys, discoveries
                reader.rec_model.content_generation(keys=[])  # competing model/basis publisher guard
                recording = RecordingModelUpdate(encoder=generation['model'], generation=generation['generation'],
                    genre_revision=generation['revision'], input_fingerprint=generation['inputs'])
                changes = reader.rec_model.prepare_training_changes({
                    'rec_content_vectors': generation['library'],
                    'rec_discovered_content_vectors': generation['discovered']},
                    recording=recording)
                serial = reader.prepared_content.serial()
                state = {'serial': serial, 'rows': rows, 'at': now, 'reason': reason,
                         'estimated_bytes': estimated, 'phase_bytes': phases,
                         'dimensions': dim, 'input_fingerprint': generation['inputs']}
                settings = {BASIS_STATE: json.dumps(state),
                            FIT_STATE: json.dumps({'state': 'ready', 'attempted_at': now,
                                'estimated_bytes': estimated, 'memory_bytes': self.budget.memory_bytes})}
                def publish():
                    self.store.rec_model.publish_training_generation(changes)
                    self.store.rec_model.publish_model_state(recording=recording, related_settings=settings)
                if snapshot.publish_ordered(publish):
                    self._record('published', reason)
                    return True
                self._defer(now, 'prepared-corpus-changed')
        except WorkBudgetExceeded:
            self._defer(now, 'fit-time-budget')
        return False

    def full_due(self, now=None):
        """A metadata write alone cannot admit an expensive complete model rebuild."""
        if self.store.rec_refresh.version() not in (None, REFRESH_VERSION):
            return True
        previous = _state(self.store, FULL_STATE)
        config = _config(self.store)
        fitted = self.store.rec_model.recording_state().fit_fingerprint
        artist_fitted = self.store.rec_model.artist_state().fit_fingerprint
        if full_due_values(previous, config, fitted, artist_fitted, 0, 0, now):
            return True
        _content, evidence = self.store.prepared_content.counters()
        changed = self.store.prepared_content.changed_since(previous['content'])
        evidence_changed = evidence - previous['evidence']
        due = full_due_values(previous, config, fitted, artist_fitted, changed, evidence_changed, now)
        if not due and (changed or evidence_changed):
            self._record('waiting', 'accumulating-evidence')
        return bool(due)

    def full_checkpoint(self, now=None):
        content, evidence = self.store.prepared_content.counters()
        return {
            'content': content, 'evidence': evidence, 'at': now,
            'rows': self.store.prepared_content.changed_since(-1),
            # The installed checkpoint format still reserves three legacy columns.
            'config': [*_config(self.store), None, None, None]}

    def completed_full(self, checkpoint):
        self.store.settings.set_setting(FULL_STATE, json.dumps(checkpoint))

    def status(self):
        """Bounded operational progress, separate from listener-facing setup copy."""
        return {**self.store.prepared_content.progress(),
                'basis': _state(self.store, BASIS_STATE),
                'full': _state(self.store, FULL_STATE),
                'fit': _state(self.store, FIT_STATE),
                'budget': vars(self.budget)}
