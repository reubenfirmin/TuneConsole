"""Budgeted worker preparation for Model Health's historical diagnostics."""
import hashlib
from uuid import uuid4

from yt_playlist.core.activity import check_cancellable_work, work_budget
from yt_playlist.rec import embedding_vectors, eval_recs, summary_inputs

SURFACE = 'model_health'
VERSION = 1
PROCESS = uuid4().hex
SECONDS = 20
MEMORY_BYTES = 256 * 1024 * 1024
MAX_TARGETS = 250


def input_key(store, now):
    # The process token makes a reopened cache explicitly last-good until this
    # connection's revision counters have been checked by its worker.
    revisions = summary_inputs.inputs(store, SURFACE, now)[:-1]
    return hashlib.sha256(repr((VERSION, PROCESS, revisions)).encode()).hexdigest()


def prepare(store, now):
    with work_budget(SECONDS), store.read_snapshot() as snapshot:
        reader = snapshot.store
        stamp = input_key(store, now)
        holdout = eval_recs.best_holdout(reader)
        inputs = (embedding_vectors.load_vectors(reader), reader.rec_query.track_content(),
                  reader.rec_query.track_audio_features())
        result = {'version': VERSION, 'inputs': stamp, 'built_at': now,
                  'holdout_days': holdout, 'recall': eval_recs.recall_at_k(reader, k=20),
                  'proj': eval_recs.projection_recall(reader, k=20, inputs=inputs, max_targets=MAX_TARGETS),
                  'temporal': eval_recs.rolling_temporal_recall(
                      reader, holdout_days=holdout, k=20, memory_bytes=MEMORY_BYTES)}
        check_cancellable_work()
        return result if snapshot.publish(
            lambda: store.rec_proposals.put_proposals(SURFACE, result, now)) else None


def view(store, now, *, failed=False):
    report = store.rec_proposals.get_proposals(SURFACE)
    if not report or report.get('version') != VERSION:
        return {'health_pending': True, 'health_ready': False, 'health_failed': failed}
    pending = report.get('inputs') != input_key(store, now)
    return {**report, 'health_ready': True, 'health_pending': pending, 'health_failed': pending and failed}
