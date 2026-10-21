"""Sync results and durable freshness, shared by manual and automatic jobs."""
import json


def resource(status, message="", **counts):
    return {"status": status, "message": message, **counts}


def combined_status(results):
    statuses = {result["status"] for result in results}
    if statuses == {"complete"}:
        return "complete"
    if "complete" in statuses or "partial" in statuses:
        return "partial"
    return "failed" if "failed" in statuses else "skipped"


def begin_attempt(store, now):
    store.set_setting("last_sync_attempt_at", str(now))


def finish_attempt(store, result, now):
    """Failed attempts must not refresh successful timestamps, including individual resources."""
    previous = json.loads(store.get_setting("library_sync_state") or "{}")

    def stamp(current, old):
        for name, item in current.items():
            item["last_attempt_at"] = now
            last_success = old.get(name, {}).get("last_success_at")
            if item["status"] == "complete":
                last_success = now
            item["last_success_at"] = last_success

    for identity_id, account in result.get("accounts", {}).items():
        old = previous.get("accounts", {}).get(identity_id, {})
        stamp(account["resources"], old.get("resources", {}))
        stamp({identity_id: account}, {identity_id: old})
    stamp(result.get("resources", {}), previous.get("resources", {}))
    result["last_attempt_at"] = now
    store.set_setting("library_sync_state", json.dumps(result))
    store.set_setting("last_sync_outcome", result["status"])
    if result["status"] == "complete":
        store.set_setting("last_sync_at", str(now))
    return result


def sync_due(store, now, max_age_s):
    # An interrupted run or a partial attempt after a recent success needs an early retry too.
    if store.get_setting("last_sync_outcome") not in (None, "complete"):
        return True
    last = store.get_setting("last_sync_at")
    attempted = store.get_setting("last_sync_attempt_at")
    if attempted and (last is None or float(attempted) > float(last)):
        return True
    state = json.loads(store.get_setting("library_sync_state") or "{}")
    if state and set(state.get("accounts", {})) != {str(i.id) for i in store.get_identities()}:
        return True
    return last is None or now - float(last) >= max_age_s
