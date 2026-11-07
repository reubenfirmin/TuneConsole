"""Compatibility entry points for bounded policy-history preparation."""


def prepare(store, *, now, limit=128):
    return store.rec_ledger.prepare_policy_history(now, limit=limit)


def maintain(store, now):
    return store.rec_ledger.maintain_policy_history(now)
