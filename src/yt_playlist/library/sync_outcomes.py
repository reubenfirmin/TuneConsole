"""Sync results and durable freshness, shared by manual and automatic jobs."""
import json
from copy import deepcopy

MAX_AGE_S = 86400
RETRY_BASE_S = 300
RETRY_MAX_S = 3600


class SyncSchedule:
    """Use persisted resource outcomes without treating a retry as a full refresh."""

    def __init__(self, store, now, *, automatic=False, retry_now=False, max_age_s=MAX_AGE_S):
        self.state = json.loads(store.settings.get_setting("library_sync_state") or "{}")
        self.now, self.automatic, self.retry_now = now, automatic, retry_now
        self.max_age_s = max_age_s

    def previous(self, identity_id, name):
        resources = (self.state.get("resources", {}) if identity_id is None else
                     self.state.get("accounts", {}).get(str(identity_id), {}).get("resources", {}))
        return resources.get(name, {})

    def due(self, item):
        if not self.automatic or not item:
            return True
        if item.get("status") != "complete":
            return self.retry_now or self.now >= item.get("retry_at", 0)
        return self.now - (item.get("last_success_at") or 0) >= self.max_age_s

    def resource_due(self, identity_id, name):
        return self.due(self.previous(identity_id, name))


def retained(item):
    """An untouched snapshot keeps both its success and attempt timestamps."""
    result = deepcopy(item)
    result["_retained"] = True
    if isinstance(result.get("items"), dict):
        result["items"] = {key: retained(child) for key, child in result["items"].items()}
    return result


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
    store.settings.set_setting("last_sync_attempt_at", str(now))


def finish_attempt(store, result, now):
    """Failed attempts must not refresh successful timestamps, including individual resources."""
    previous = json.loads(store.settings.get_setting("library_sync_state") or "{}")

    def stamp(current, old):
        for name, item in current.items():
            previous_item = old.get(name, {})
            if isinstance(item.get("items"), dict):
                stamp(item["items"], previous_item.get("items", {}))
            if item.pop("_retained", False):
                for field in ("last_attempt_at", "last_success_at", "failures", "retry_at"):
                    if field in previous_item:
                        item[field] = previous_item[field]
                continue
            item["last_attempt_at"] = now
            last_success = previous_item.get("last_success_at")
            if item["status"] == "complete":
                last_success = now
                item["failures"] = 0
                item.pop("retry_at", None)
            else:
                failures = min(previous_item.get("failures", 0) + 1, 16)
                item["failures"] = failures
                item["retry_at"] = now + min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (failures - 1))
            item["last_success_at"] = last_success

    for identity_id, account in result.get("accounts", {}).items():
        old = previous.get("accounts", {}).get(identity_id, {})
        stamp(account["resources"], old.get("resources", {}))
        stamp({identity_id: account}, {identity_id: old})
    stamp(result.get("resources", {}), previous.get("resources", {}))
    result["last_attempt_at"] = now
    store.settings.set_setting("library_sync_state", json.dumps(result))
    store.settings.set_setting("last_sync_outcome", result["status"])
    if result["status"] == "complete":
        store.settings.set_setting("last_sync_at", str(now))
    return result


def sync_due(store, now, max_age_s, *, retry_now=False):
    schedule = SyncSchedule(store, now, automatic=True, retry_now=retry_now, max_age_s=max_age_s)
    state = schedule.state
    configured = {str(i.id) for i in store.identities.get_identities()}
    if state:
        if set(state.get("accounts", {})) != configured:
            return True
        for account in state.get("accounts", {}).values():
            resources = account.get("resources", {})
            # Ratings require trustworthy playlist evidence; the album mirror is
            # derived from account snapshots. Neither schedules a redundant scan.
            for name in ("playlist_listing", "history", "saved_albums"):
                if schedule.due(resources.get(name, {})):
                    return True
            # Without a complete listing, child retries cannot run. Wait for
            # that listing's deadline rather than creating ineffective jobs.
            if resources.get("playlist_listing", {}).get("status") != "complete":
                continue
            playlists = resources.get("playlists", {})
            if not playlists.get("items") and playlists.get("status") != "complete":
                if schedule.due(playlists):
                    return True
            if any(schedule.due(item) for item in playlists.get("items", {}).values()):
                return True
        if schedule.resource_due(None, "album_tracks"):
            return True
        # A process stopped between beginning and publishing an attempt. Retry
        # once after a bounded delay, rather than spinning on every poll.
        attempted = store.settings.get_setting("last_sync_attempt_at")
        if attempted and float(attempted) > state.get("last_attempt_at", 0):
            return retry_now or now >= float(attempted) + RETRY_BASE_S
        return False
    last = store.settings.get_setting("last_sync_at")
    attempted = store.settings.get_setting("last_sync_attempt_at")
    if attempted and (last is None or float(attempted) > float(last)):
        return retry_now or now >= float(attempted) + RETRY_BASE_S
    return last is None or now - float(last) >= max_age_s
