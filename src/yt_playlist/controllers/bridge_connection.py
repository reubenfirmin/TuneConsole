"""Reconcile local account state when the extension establishes a connection."""


class BridgeConnection:
    def __init__(self, settings, *, guard, paused, clear_authentication, wake):
        self.settings, self.guard, self.paused = settings, guard, paused
        self.clear_authentication, self.wake = clear_authentication, wake

    def record(self):
        # Pairing and authentication share the writer's pause boundary. Sync
        # must only wake after reconciliation has finished and released it.
        with self.guard:
            if self.settings is not None and not self.paused():
                self.settings.set_setting('bridge_paired', '1')
            self.clear_authentication()
        self.wake()
