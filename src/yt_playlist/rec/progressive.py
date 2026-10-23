"""Progressive content preparation using the established encoder and clustering.

Accepted per-recording inputs form a durable corpus. A basis fit consumes that
corpus, not live provider rows; its publication still uses strict snapshot
guards. Every vector in a publication uses the exact same encoder, including
its audio normalization. Last-good models remain available between steps.
"""
from dataclasses import dataclass
import json
import math
import uuid

from yt_playlist.core.activity import check_stopping, work_budget, WorkBudgetExceeded
from yt_playlist.rec import content_model, rec_params, taste_modes
from yt_playlist.repos.rec_model import VectorChanges
from yt_playlist.util import genre_catalog

PHASE = 'rec: progressive content'
BASIS_STATE = 'rec_preparation_basis'
FULL_STATE = 'rec_preparation_full'
MODE_PARAMS = ('modes_k', 'modes_min_members', 'modes_match_threshold')


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
        self.retry_fit_at = 0

    def _record(self, outcome, reason):
        self.store.maintenance_diagnostics.record(outcome, reasons=(reason,), phase=PHASE)

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
        return (self.store.prepared_content.has_pending()
                or (now >= self.retry_fit_at and self._basis_reason(now) is not None))

    def capture(self, now):
        pending = self.store.prepared_content.pending(self.batch)
        if not pending:
            return False
        try:
            with work_budget(self.budget.capture_seconds), self.store.read_snapshot() as snapshot:
                reader = snapshot.store
                rows = reader.prepared_content.capture(pending)
                raw_model = reader.settings.get_setting('rec_content_model')
                revision = reader.settings.get_setting('rec_content_genre_revision')
                changes = _delta(reader, rows, raw_model if revision == genre_catalog.REVISION else None)
                # Guard a competing publisher even when this batch has no vector changes.
                reader.settings.get_setting('rec_content_generation')
                def publish():
                    self.store.prepared_content.publish_capture(rows, now)
                    if changes:
                        self.store.rec_model.publish_training_generation(changes)
                        self.store.settings.publish_values({
                            'rec_content_generation': uuid.uuid4().hex,
                            'rec_content_inputs': '',  # not a claim of a current full-corpus fit
                        })
                if snapshot.publish(publish):
                    self.batch = min(self.budget.batch, max(1, self.batch * 2))
                    self._record('published', 'changed-entity-inputs')
                    return True
        except WorkBudgetExceeded:
            self._record('deferred', 'capture-budget')
        self.batch = max(1, self.batch // 2)
        self.store.prepared_content.rotate(pending)
        self._record('deferred', 'hot-input-batch')
        return False

    def fit(self, now):
        """Fit a coherent basis only after useful accumulated changes, within a budget."""
        if now < self.retry_fit_at:
            return False
        raw_model = self.store.settings.get_setting('rec_content_model')
        reason = self._basis_reason(now)
        if reason is None:
            return False
        self.retry_fit_at = now + self.budget.fit_interval
        try:
            with work_budget(self.budget.fit_seconds), self.store.read_snapshot() as snapshot:
                reader = snapshot.store
                if raw_model and reader.prepared_content.missing_previous_inputs():
                    self._record('waiting', 'previous-generation-inputs')
                    return False
                corpus = reader.prepared_content.corpus(self.budget.max_rows)
                if corpus is None:
                    self._record('deferred', 'corpus-memory-budget')
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
                    self._record('waiting', 'empty-corpus')
                    return False
                model = content_model.build_content_model(content, audio)
                # Budget detached Python inputs plus simultaneous float32/float64
                # matrices used by the existing encoder and k-means implementation.
                dim = model['ncat'] + len(model['cont'])
                estimated = len(corpus) * 2048 + (len(keys) + len(discoveries)) * dim * 32
                if estimated > self.budget.memory_bytes:
                    self._record('deferred', 'matrix-memory-budget')
                    return False
                inputs = content_model.ContentInputs(keys, content, audio)
                encoded_keys, matrix, model = content_model.build_library_content(inputs)
                generation = {'model': json.dumps(model), 'generation': uuid.uuid4().hex,
                    'revision': genre_catalog.REVISION, 'inputs': content_model.input_fingerprint(inputs),
                    'library': [(key, matrix[i].tobytes()) for i, key in enumerate(encoded_keys)],
                    'discovered': content_model.encode_discovered(discoveries, model)}
                reader.rec_model.content_generation(keys=[])  # competing model/basis publisher guard
                modes = taste_modes.prepare_modes(reader, now, snapshot=generation,
                                                  genres={key: value[0] for key, value in content.items()})
                changes = reader.rec_model.prepare_training_changes({
                    'rec_content_vectors': generation['library'],
                    'rec_discovered_content_vectors': generation['discovered']})
                serial = reader.prepared_content.serial()
                state = {'serial': serial, 'rows': len(corpus), 'at': now, 'reason': reason,
                         'modes': [rec_params.get_param(reader, key) for key in MODE_PARAMS],
                         'estimated_bytes': estimated, 'input_fingerprint': generation['inputs']}
                settings = {'rec_content_model': generation['model'],
                            'rec_content_generation': generation['generation'],
                            'rec_content_genre_revision': generation['revision'],
                            'rec_content_inputs': generation['inputs'], BASIS_STATE: json.dumps(state)}
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
                self._record('deferred', 'prepared-corpus-changed')
        except WorkBudgetExceeded:
            self._record('deferred', 'fit-time-budget')
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
                'budget': vars(self.budget)}
