"""Application access to the separately composed playback inbox."""


def inbox(store):
    return getattr(store, '_evidence_source', store)._playback_feedback


def deferred(store):
    return bool(store.activity_paused or store.playback_feedback.pending())


def capture(store, kind, identity, body, now):
    return store.playback_feedback.capture(kind, identity, body, now)
