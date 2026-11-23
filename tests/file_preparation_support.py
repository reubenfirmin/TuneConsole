"""Exercise the current policy history maintenance path."""
from types import SimpleNamespace


policy_history = SimpleNamespace(prepare=lambda store, *, now, limit=128:
    store.rec_ledger.prepare_policy_history(now, limit=limit))
