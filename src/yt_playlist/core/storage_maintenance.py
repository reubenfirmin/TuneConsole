"""Status for recommendation retirement and retained listening feedback."""

OPTIMIZING_BANNER = 'Optimizing app storage; some recommendations are temporarily unavailable.'
RETIREMENT_MESSAGE = 'Cleaning up old recommendations. Some changes are paused.'
EVIDENCE_MESSAGE = 'Listening-history cleanup is continuing in the background. Your library remains available.'
EVIDENCE_RETRY_MESSAGE = 'Listening-history cleanup needs another attempt. Your library remains available.'


class StorageMaintenance:
    def __init__(self, store):
        self.store = store

    def status(self):
        store = self.store
        running = bool(getattr(store, '_evidence_maintenance', 0))
        evidence = running or bool(getattr(store, '_evidence_recovery_pending', False))
        retrying = evidence and getattr(store, '_evidence_recovery_failed', False)
        active = bool(getattr(store, '_recommendation_retirement_pending', False)
                      or getattr(store, 'activity_paused', False))
        pending = store.playback_feedback.pending()
        if active:
            phase = 'recommendation_retirement'
            message = RETIREMENT_MESSAGE
        elif evidence:
            phase = getattr(store, '_evidence_phase', 'listening_history')
            message = EVIDENCE_RETRY_MESSAGE if retrying else EVIDENCE_MESSAGE
            detail = {'inventory': 'Checking saved recommendation contexts.',
                      'checkpoint': 'Checkpointing retained listening evidence.',
                      'retention': 'Reclaiming acknowledged diagnostic storage.'}.get(phase)
            if detail:
                message += ' ' + detail
        elif pending:
            phase = 'playback_feedback'
            message = 'Applying saved playback feedback. Your library remains available.'
        else:
            phase, message = 'idle', ''
        if pending:
            message += f' {pending} saved playback observations await replay.'
        return dict(active=active, phase=phase, message=message, pending_feedback=pending,
                    recording=not active, capturing_feedback=True, warning=bool(retrying),
                    banner_message=OPTIMIZING_BANNER if active and not retrying else '',
                    progress=dict(getattr(store, '_recommendation_retirement_progress', {})),
                    error=getattr(store, '_recommendation_retirement_error', ''),
                    evidence_maintenance=dict(pending=evidence, running=running,
                        phase=getattr(store, '_evidence_phase', 'inventory'), retrying=retrying,
                        progress=dict(getattr(store, '_evidence_progress', {}))))
